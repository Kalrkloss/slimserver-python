"""
Media folder scanner for Pyrion Music Server.
Modified to scan /mnt/media2/Musik instead of current source paths.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiosqlite
from mutagen import File as MutagenFile
from mutagen.apev2 import APEv2File
from mutagen.flac import FLAC
from mutagen.id3 import ID3FileType
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus

if TYPE_CHECKING:
    from lyrion.config import LyrionConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported extensions
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    "mp3", "flac", "ogg", "oga", "m4a", "aac", "aiff", "aif",
    "wav", "wma", "opus", "spx", "ape", "tak", "m4b", "mpc",
    "mp+", "ogg", "opus",
})

# Hidden / system files and patterns to skip
SKIP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\."),
    re.compile(r"^desktop\.ini$", re.I),
    re.compile(r"^thumbs\.db$", re.I),
    re.compile(r"^\$"),
    re.compile(r"~.tmp$"),
    re.compile(r"\.tmp$"),
    re.compile(r"\.download$"),
    re.compile(r"\.part$"),
]

# Folder-level cover image names (in priority order)
COVER_NAMES: list[str] = [
    "cover.jpg", "cover.jpeg", "cover.png", "cover.gif", "cover.webp",
    "folder.jpg", "folder.jpeg", "folder.png",
    "album.jpg", "album.jpeg", "album.png",
    "front.jpg",
]


def _tag_value(tags: Any, *keys: str) -> str:
    """Return the first non-empty tag value for any of the given keys.

    Handles the different mutagen tag container styles:
    - ID3 frames (expose ``.text`` / ``.strings``)
    - Vorbis comments / APE tags (``list[str]`` values)
    - MP4 atoms (single ``str`` values)
    """
    if not tags:
        return ""
    for key in keys:
        try:
            val = tags.get(key)
        except Exception:  # noqa: BLE001
            continue
        if val is None:
            continue
        if hasattr(val, "text"):
            val = val.text
        if isinstance(val, (list, tuple)):
            if not val:
                continue
            val = val[0]
        val = str(val).strip()
        if val and val.lower() not in ("", "none", "unknown"):
            return _fix_tag_encoding(val)
    return ""


def _fix_tag_encoding(value: str) -> str:
    """Recover tag text that was decoded with the wrong single-byte charset.

    Perl parity: ``Slim::Utils::Unicode::utf8decode_guess($string,
    @preferedEncodings)`` (``Slim/Utils/Unicode.pm:154-186``) tries the
    preferred encodings strictly and then a charset detector
    (``encodingFromString`` → ``Encode::Detect::Detector::detect``,
    ``Unicode.pm:482-503``). LMS runs tag strings through it, so a tag whose
    bytes are CP1251 but whose ID3v2 encoding byte says Latin-1 comes out as
    readable Cyrillic instead of mojibake.

    Our scanner reads tags with mutagen, which trusts that encoding byte: a
    Russian ID3v2 tag arrives as 'ßðèëî' (the latin-1 reading of the CP1251
    bytes of 'Ярило'). The live library had 34 titles, 2 albums and 1
    contributor like this (2026-09-12).

    Deliberately conservative — a wrong "repair" would corrupt correct tags
    (French 'Leçon de ténèbres', Finnish 'Jäästä Syntynyt', Turkish 'Gökhan
    Özoguz' all share the U+00C0-U+00FF range), so a candidate is only
    accepted when

    * every character is latin-1 encodable (a real Unicode string is already
      fine and returned untouched), and
    * at least 3 characters are >= U+0080 and they make up >= 50 % of the
      string (mojibake is dominated by them; real words are mostly ASCII),
      and
    * the CP1251 reading of those bytes is mostly letters in the Cyrillic
      block (ASCII-art titles and symbol runs are rejected by this).

    UTF-8 read as latin-1 ('Ã©' → 'é') is repaired first, because that is the
    other common mis-decode and it is unambiguous (strict UTF-8 only).
    """
    if not value:
        return value
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError:
        return value                      # genuine Unicode (Cyrillic, CJK, ...)
    if raw.isascii():
        return value

    # 1) UTF-8 bytes that were read as latin-1/cp1252.
    try:
        utf8_candidate = raw.decode("utf-8")
    except UnicodeDecodeError:
        utf8_candidate = ""
    if utf8_candidate and utf8_candidate != value and any(
            ord(ch) > 0x7F for ch in utf8_candidate):
        # Only accept when the result is not just the same bytes turned into
        # Latin-1 letters (i.e. real multi-byte sequences were recovered).
        if utf8_candidate != raw.decode("latin-1"):
            return utf8_candidate

    # 2) A single-byte charset that is not latin-1 (CP1251 Cyrillic here).
    high = [ch for ch in value if ord(ch) >= 0x80]
    if len(high) < 3 or len(high) / len(value) < 0.5:
        return value
    candidate = raw.decode("cp1251", errors="replace")
    cyrillic = sum(1 for ch in candidate if "\u0400" <= ch <= "\u04FF")
    letters = sum(1 for ch in candidate if ch.isalpha())
    if cyrillic >= 3 and letters and cyrillic / letters >= 0.8:
        return candidate
    return value

# ---------------------------------------------------------------------------
# Scan configuration
# ---------------------------------------------------------------------------

@dataclass
class ScanConfig:
    """Configuration for scanning music folders."""
    base_path: Path | None = None
    extensions: frozenset[str] = SUPPORTED_EXTENSIONS
    skip_patterns: list[re.Pattern[str]] = field(default_factory=list)
    recursive: bool = True
    max_depth: int = 20
    extract_metadata: bool = True
    generate_artwork: bool = True

    def __post_init__(self) -> None:
        if self.base_path is None:
            # No hardcoded Linux path: the folder comes from the preference
            # chain (media/music_dir.py → Perl's defaultMediaDirs,
            # ``Slim/Utils/Prefs.pm:687-712`` → ``OSDetect::dirsFor('music')``).
            from lyrion.media.music_dir import resolve_music_dir

            self.base_path = resolve_music_dir()
            if self.base_path is not None:
                logger.info("Configured to scan: %s", self.base_path)
            return
        from lyrion.platform import paths as platform_paths

        code, message = platform_paths.explain_missing_path(self.base_path)
        if code != platform_paths.OK:
            # Catches the gvfs case too: "mount gone" is not the same as
            # "folder deleted" and must not look like a successful empty scan.
            logger.warning("Scan base path unusable (%s): %s", code, message)
            return
        logger.info("Configured to scan: %s", self.base_path)

# ---------------------------------------------------------------------------
# Scan state and statistics
# ---------------------------------------------------------------------------

@dataclass
class ScanStats:
    """Statistics for a scan operation."""
    total_files: int = 0
    total_folders: int = 0
    scanned_files: int = 0
    processed_files: int = 0
    skipped_files: int = 0
    error_files: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

# ---------------------------------------------------------------------------
# Media information dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """Result of scanning a single file."""
    path: Path
    filename: str
    relative_path: str
    filesize: int
    mimetype: str
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    year: int = 0
    track: int = 0
    duration: int = 0
    bitrate: int = 0
    sample_rate: int = 0
    channels: int = 0
    artwork_path: Path | None = None
    # ReplayGain-Tags (Perl Schema.pm:2915-2945 gemungt). Track-Werte gehören
    # an tracks.replay_gain/-peak, Album-Werte an albums.replay_gain/-peak.
    replay_gain: float | None = None
    replay_peak: float | None = None
    album_replay_gain: float | None = None
    album_replay_peak: float | None = None
    last_modified: datetime = field(default_factory=datetime.now)
    added_time: datetime = field(default_factory=datetime.now)
    checksum: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for database insertion."""
        return {
            "filename": self.filename,
            "relative_path": self.relative_path,
            "filesize": self.filesize,
            "mimetype": self.mimetype,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "genre": self.genre,
            "year": self.year,
            "track": self.track,
            "duration": self.duration,
            "bitrate": self.bitrate,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "artwork_path": str(self.artwork_path) if self.artwork_path else None,
            "last_modified": self.last_modified.isoformat(),
            "added_time": self.added_time.isoformat(),
            "checksum": self.checksum,
        }

@dataclass
class ScanProgress:
    """Progress reporting for scanning."""
    current_file: Path | None = None
    current_depth: int = 0
    files_processed: int = 0
    total_files: int = 0
    start_time: datetime | None = None

# ---------------------------------------------------------------------------
# Main scanner class
# ---------------------------------------------------------------------------

class MediaScanner:
    """Scanner for music files and folders."""

    def __init__(self, config: ScanConfig | None = None) -> None:
        self.config = config or ScanConfig()
        self.stats = ScanStats()
        self.progress = ScanProgress()

    async def scan(self, progress_callback: Callable[[ScanProgress], Awaitable[None]] | None = None) -> tuple[list[ScanResult], ScanStats]:
        """Scan the configured base path for music files.

        Args:
            progress_callback: Optional async callback for progress updates

        Returns:
            Tuple of (list of scan results, scan statistics)
        """
        logger.info("Starting scan of: %s", self.config.base_path)
        self.stats.start_time = datetime.now()

        if self.config.base_path is None:
            # Nothing to walk: no 'mediadirs'/'musicdir'/'audiodir' set and no
            # OS music folder found (Perl defaultMediaDirs,
            # ``Slim/Utils/Prefs.pm:687-712``).  Refuse instead of walking a
            # hardcoded Linux path that may exist on the build machine only.
            logger.error(
                "No music folder to scan — set 'mediadirs' or 'musicdir' "
                "(Perl: defaultMediaDirs, Slim/Utils/Prefs.pm:687-712)")
            self.stats.errors.append("no music folder configured")
            return [], self.stats

        scan_results: list[ScanResult] = []

        # Process files with a bounded pool of concurrent workers. The
        # per-file work is CPU/IO heavy (mutagen parsing, artwork lookup),
        # so 8 parallel workers give a large speedup on big libraries.
        CONCURRENCY = 8
        sem = asyncio.Semaphore(CONCURRENCY)
        pending: list[asyncio.Task] = []

        async def _process_limited(file_path: Path) -> ScanResult | None:
            async with sem:
                try:
                    return await self._process_file(file_path)
                except Exception as e:
                    error_msg = f"Error processing {file_path}: {str(e)}"
                    logger.error(error_msg)
                    self.stats.error_files += 1
                    self.stats.errors.append(error_msg)
                    return None

        async def _drain() -> None:
            for task in pending:
                result = await task
                if result:
                    scan_results.append(result)
                    self.stats.processed_files += 1
            pending.clear()

        # Recursively walk through directories
        async for root, dirs, files in self._walk_dir_async(self.config.base_path):
            self.stats.total_folders += len(dirs)
            self.stats.total_files += len(files)

            # Process files in this directory
            for file in files:
                self.stats.scanned_files += 1
                file_path = root / file

                # Update progress
                if progress_callback:
                    self.progress.current_file = file_path
                    self.progress.files_processed += 1
                    self.progress.current_depth = len(root.relative_to(self.config.base_path).parts)
                    await progress_callback(self.progress)

                # Skip files matching skip patterns
                if self._should_skip_file(file_path):
                    self.stats.skipped_files += 1
                    continue

                pending.append(asyncio.create_task(_process_limited(file_path)))
                # Keep a bounded queue so we don't hold every file in memory
                if len(pending) >= CONCURRENCY * 8:
                    await _drain()

        await _drain()
        await self._finalize_scan(scan_results)
        logger.info("Scan completed. Processed: %d, Errors: %d", self.stats.processed_files, self.stats.error_files)
        return scan_results, self.stats

    async def _walk_dir_async(self, root: Path, depth: int = 0):
        """Non-recursive async directory walk using os.walk (fast)."""
        if depth > self.config.max_depth:
            return
        try:
            for dirpath, dirnames, filenames in root.walk(top_down=True):
                # Respect max depth
                rel_depth = len(Path(dirpath).relative_to(self.config.base_path).parts)
                if self.config.recursive and rel_depth > self.config.max_depth:
                    del dirnames[:]  # don't recurse deeper
                    continue
                # Yield files in this directory
                for fname in filenames:
                    yield Path(dirpath), [], [fname]
                # Yield directories
                for dname in dirnames:
                    d = Path(dirpath) / dname
                    yield d, [], []
        except PermissionError:
            return

    def _should_skip_file(self, file_path: Path) -> bool:
        """Check if a file should be skipped based on configuration."""
        filename = file_path.name.lower()

        # Check extension
        if not self._has_supported_extension(file_path):
            return True

        # Check skip patterns
        for pattern in self.config.skip_patterns:
            if pattern.search(filename):
                return True

        return False

    def _has_supported_extension(self, file_path: Path) -> bool:
        """Check if file has a supported audio extension."""
        return file_path.suffix.lower().lstrip('.') in self.config.extensions

    async def scan_single_file(self, file_path: Path) -> ScanResult | None:
        """Extract metadata for a single file (no DB write, no directory walk).

        Public wrapper around _process_file for the importer.
        """
        return await self._process_file(file_path)

    def _extract_tags(self, file_path: Path) -> tuple:
        """mutagen tag extraction — sync, runs in a worker thread.

        Format-agnostic tag lookup: ID3 frame IDs, Vorbis/APE-style
        keys, MP4 atom names. Returns the ten metadata values.
        """
        title = artist = album = genre = ""
        year = track = duration = bitrate = sample_rate = channels = 0
        rg_gain = rg_peak = rg_album_gain = rg_album_peak = None
        try:
            audio_file = MutagenFile(file_path)
            if audio_file is None:
                logger.warning("No audio metadata for %s", file_path)
                return (title, artist, album, genre, year, track,
                        duration, bitrate, sample_rate, channels,
                        rg_gain, rg_peak, rg_album_gain, rg_album_peak)
            try:
                tags = getattr(audio_file, "tags", None)
                info = getattr(audio_file, "info", None)
                title = _tag_value(tags, "TIT2", "title", "\xa9nam", "Title", "WM/Title")
                artist = _tag_value(
                    tags, "TPE1", "artist", "\xa9ART", "Author",
                    "Album Artist", "WM/AlbumArtist",
                )
                album = _tag_value(tags, "TALB", "album", "\xa9alb",
                                   "WM/AlbumTitle", "Album")
                genre = _tag_value(tags, "TCON", "genre", "\xa9gen",
                                   "WM/Genre", "Genre")
                year_str = _tag_value(tags, "TDRC", "TYER", "date", "\xa9day",
                                      "WM/Year", "Year")
                if year_str:
                    m = re.search(r"\d{4}", year_str)
                    if m:
                        year = int(m.group(0))
                track_str = _tag_value(tags, "TRCK", "tracknumber", "trck",
                                       "Track", "WM/TrackNumber")
                if track_str:
                    m = re.search(r"\d+", track_str)
                    if m:
                        track = int(m.group(0))
                # ReplayGain (Perl Schema.pm:2915-2945 über die Tabellen der
                # Formatklassen, s. lyrion/media/replaygain.py).
                from lyrion.media.replaygain import extract_replaygain

                (rg_gain, rg_peak, rg_album_gain, rg_album_peak) = extract_replaygain(tags)
                if info is not None:
                    duration = int(getattr(info, "length", 0) * 1000) or 0
                    bitrate = int(getattr(info, "bitrate", 0) or 0)
                    sample_rate = int(getattr(info, "sample_rate", 0) or 0)
                    channels = int(getattr(info, "channels", 0) or 0)
            finally:
                # Close file handles for types that support it
                try:
                    audio_file.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Failed to extract metadata from %s: %s", file_path, e)
        return (title, artist, album, genre, year, track,
                duration, bitrate, sample_rate, channels,
                rg_gain, rg_peak, rg_album_gain, rg_album_peak)

    async def _process_file(self, file_path: Path) -> ScanResult | None:
        """Process a single music file."""
        # Get file stats
        try:
            stat = await asyncio.to_thread(file_path.stat)
        except OSError as e:
            logger.error("Failed to stat %s: %s", file_path, e)
            return None

        # Determine relative path from base
        try:
            relative_path = file_path.relative_to(self.config.base_path)
        except ValueError:
            relative_path = file_path

        # Determine MIME type
        import mimetypes
        mime_type, _ = mimetypes.guess_type(str(file_path))

        # Extract metadata using mutagen. The mutagen C bindings release
        # the GIL while parsing, so run the extraction in a worker thread
        # — the 8 concurrent scan workers then parallelize for real.
        (title, artist, album, genre, year, track,
         duration, bitrate, sample_rate, channels,
         replay_gain, replay_peak,
         album_replay_gain, album_replay_peak) = (
            await asyncio.to_thread(self._extract_tags, file_path))

        # Metadata heuristics: fill gaps from folder structure and the
        # filename (tags always win). Many files carry only partial tags
        # or none at all — the goal is no track without artist/album.
        try:
            from lyrion.media.heuristics import apply_heuristics
            guessed = apply_heuristics(
                file_path=file_path,
                library_root=str(self.config.base_path),
                title=title, artist=artist, album=album,
                genre=genre, year=year, track=track,
            )
            title, artist, album = guessed.title, guessed.artist, guessed.album
            genre, year, track = guessed.genre, guessed.year, guessed.track
        except Exception as exc:  # noqa: BLE001
            logger.debug("Heuristics failed for %s: %s", file_path, exc)

        # Look for cover artwork
        artwork_path = None
        if self.config.generate_artwork:
            try:
                # Try to find artwork in same folder as file
                folder_artwork = await self._find_artwork_in_folder(file_path.parent)
                if folder_artwork:
                    artwork_path = folder_artwork
            except Exception as e:
                logger.debug("No artwork found for %s", file_path)

        # Calculate checksum
        checksum = ""
        try:
            checksum = await self._calculate_file_hash(file_path)
        except Exception as e:
            logger.debug("Could not calculate checksum for %s: %s", file_path, e)

        # Create scan result
        result = ScanResult(
            path=file_path,
            filename=file_path.name,
            relative_path=str(relative_path),
            filesize=stat.st_size,
            mimetype=mime_type or "application/octet-stream",
            title=title,
            artist=artist,
            album=album,
            genre=genre,
            year=year,
            track=track,
            duration=duration,
            bitrate=bitrate,
            sample_rate=sample_rate,
            channels=channels,
            artwork_path=artwork_path,
            replay_gain=replay_gain,
            replay_peak=replay_peak,
            album_replay_gain=album_replay_gain,
            album_replay_peak=album_replay_peak,
            last_modified=datetime.fromtimestamp(stat.st_mtime),
            added_time=datetime.fromtimestamp(stat.st_ctime),
            checksum=checksum,
        )

        return result

    async def _find_artwork_in_folder(self, folder: Path) -> Path | None:
        """Find cover artwork in a folder."""
        for cover_name in COVER_NAMES:
            artwork_path = folder / cover_name
            if artwork_path.exists() and artwork_path.is_file():
                logger.debug("Found artwork: %s", artwork_path)
                return artwork_path
        # Fallback: any image whose name contains 'cover'/'folder'/'front'/
        # 'albumart' (e.g. '00-artist-album-cover.jpg' release layouts).
        try:
            for entry in folder.iterdir():
                if not entry.is_file():
                    continue
                n = entry.name.lower()
                if n.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")) and any(
                        k in n for k in ("cover", "folder", "front", "albumart")):
                    return entry
        except OSError:
            pass
        return None

    async def _calculate_file_hash(self, file_path: Path) -> str:
        """Return a fast change-detection signature for a file.

        Real LMS tracks files by size + mtime rather than hashing the full
        content; hashing every file makes a rescan of a large library take
        hours. The signature is stable per file version and O(1).
        """
        try:
            stat = file_path.stat()
            return f"{stat.st_size}-{int(stat.st_mtime)}"
        except Exception as e:
            logger.debug("Error calculating signature for %s: %s", file_path, e)
            return ""

    async def _finalize_scan(self, scan_results: list[ScanResult]) -> None:
        """Finalize scan and update statistics."""
        # Count files by status
        for result in scan_results:
            if result.error:
                self.stats.error_files += 1
            else:
                self.stats.added += 1

        self.stats.processed_files = len(scan_results)
