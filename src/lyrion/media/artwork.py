"""Artwork extraction, discovery, resizing, and caching for Lyrion Music Server."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
from PIL import Image

if TYPE_CHECKING:
    from lyrion.media.scanner import ScannedTrack

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Artwork sizes (width x height) used by various player displays
# ---------------------------------------------------------------------------

ARTWORK_SIZES: list[tuple[int, int]] = [
    (32, 32),    # Squeezebox small display / list icons
    (64, 64),    # Squeezebox medium
    (128, 128),  # full-color thumbnails
    (200, 200),  # standard cover display
    (400, 400),  # high-resolution
    (800, 800),  # full-size original
]

# File names scanned for in the album folder (in priority order)
COVER_NAMES: list[str] = [
    "cover.jpg", "cover.jpeg", "cover.png", "cover.gif", "cover.webp",
    "folder.jpg", "folder.jpeg", "folder.png",
    "album.jpg", "album.jpeg", "album.png",
    "front.jpg", "front.jpeg", "front.png",
    "default.jpg", "default.png",
]

# JPEG quality for cached artwork
JPEG_QUALITY = 85


@dataclass
class ArtworkResult:
    """Result of an artwork lookup for a single track or album."""
    found: bool = False
    source: str = ""  # "embedded", "folder", "cache", "default"
    cache_path: Path | None = None
    sizes: dict[tuple[int, int], Path] = field(default_factory=dict)
    mime_type: str = "image/jpeg"
    width: int = 0
    height: int = 0
    hash: str = ""


class ArtworkHandler:
    """
    Handles artwork extraction, folder scanning, resizing, and caching.

    Artwork sources (in priority order):
      1. Embedded in audio file (mutagen)
      2. Cover image file in the album folder
      3. Cached artwork from previous extraction
      4. Global default artwork

    Resizing
    --------
    Artwork is resized to fixed pixel sizes and stored in the cache directory
    as JPEG files. Only sizes that are actually requested are generated
    (lazy generation).
    """

    def __init__(
        self,
        cache_dir: Path,
        db_path: Path,
        default_artwork: Path | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.artwork_cache_dir = self.cache_dir / "artwork"
        self.artwork_cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path)
        self.default_artwork = default_artwork
        self._memory_cache: dict[str, ArtworkResult] = {}

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def get_artwork(
        self,
        track_path: Path | None = None,
        album: str | None = None,
        artist: str | None = None,
        *,
        size: int | None = None,
        sizes: list[int] | None = None,
    ) -> ArtworkResult:
        """
        Return artwork for a track or album.

        Parameters
        ----------
        track_path
            Path to an audio file. If provided, tries embedded → folder.
        album
            Album name for folder-based lookup.
        artist
            Artist name (used for folder name heuristics).
        size
            If provided, return only the artwork resized to the nearest
            cached size ≥ this value.
        sizes
            List of target widths. Artwork will be resized to each width
            and cached. The image is returned at its largest requested size.

        Returns
        -------
        ArtworkResult
        """
        # Try memory cache first
        cache_key = self._cache_key(track_path, album, artist)
        if cache_key in self._memory_cache:
            result = self._memory_cache[cache_key]
            return self._filter_sizes(result, size, sizes)

        # Try DB cache
        result = await self._load_from_db_cache(track_path, album, artist)
        if result and result.found:
            self._memory_cache[cache_key] = result
            return self._filter_sizes(result, size, sizes)

        # Extract / discover
        if track_path:
            result = await self._discover_artwork(track_path)
        elif album:
            result = await self._discover_artwork_by_album(album, artist)
        else:
            result = ArtworkResult()

        # Serve default if nothing found
        if not result.found and self.default_artwork and self.default_artwork.exists():
            result = await self._from_file(self.default_artwork, source="default")
            result.cache_path = self.default_artwork

        if result.found and result.cache_path:
            # Store in DB cache
            await self._save_to_db_cache(cache_key, result)
            self._memory_cache[cache_key] = result

        return self._filter_sizes(result, size, sizes)

    async def extract_embedded(self, track_path: Path) -> bytes | None:
        """Extract the first embedded picture from an audio file."""
        try:
            return await self._extract_embedded_bytes(track_path)
        except Exception:  # noqa: BLE001
            logger.debug("Could not extract embedded artwork from %s", track_path)
        return None

    async def scan_folder(self, folder: Path) -> Path | None:
        """Find a cover image in the given folder."""
        for name in COVER_NAMES:
            candidate = folder / name
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    async def ensure_sizes(self, source_image: Path, sizes: list[int]) -> dict[tuple[int, int], Path]:
        """Resize a source image to the given widths and cache them."""
        return await self._resize_and_cache(source_image, sizes)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _cache_key(
        self,
        track_path: Path | None,
        album: str | None,
        artist: str | None,
    ) -> str:
        """Build a cache key for this artwork lookup."""
        parts = [
            str(track_path) if track_path else "",
            album or "",
            artist or "",
        ]
        return hashlib.md5("|".join(parts).encode()).hexdigest()

    def _filter_sizes(
        self,
        result: ArtworkResult,
        size: int | None,
        sizes: list[int] | None,
    ) -> ArtworkResult:
        """Filter result to only include requested sizes."""
        if not result.sizes:
            return result
        if size is not None:
            # Find nearest larger size
            best = min(
                result.sizes.keys(),
                key=lambda s: s[0] if s[0] >= size else float("inf"),
            )
            if best[0] >= size:
                filtered = ArtworkResult(
                    found=True,
                    source=result.source,
                    cache_path=result.sizes[best],
                    sizes={best: result.sizes[best]},
                    mime_type=result.mime_type,
                    width=best[0],
                    height=best[1],
                    hash=result.hash,
                )
                return filtered
            return ArtworkResult()
        return result

    async def _discover_artwork(self, track_path: Path) -> ArtworkResult:
        """Discover artwork for a track: embedded first, then folder."""
        folder = track_path.parent

        # 1. Try embedded
        embedded_bytes = await self._extract_embedded_bytes(track_path)
        if embedded_bytes:
            source = self.artwork_cache_dir / f"{track_path.stem}_embedded.jpg"
            with open(source, "wb") as f:
                f.write(embedded_bytes)
            return await self._process_and_cache(source, source="embedded")

        # 2. Try folder scan
        cover_path = await self.scan_folder(folder)
        if cover_path:
            return await self._process_and_cache(cover_path, source="folder")

        return ArtworkResult()

    async def _discover_artwork_by_album(self, album: str, artist: str | None) -> ArtworkResult:
        """Find artwork for an album by folder scan (album name lookup)."""
        # This is a placeholder for a smarter album→folder lookup.
        # In practice, this requires the album path from the DB.
        return ArtworkResult()

    async def _extract_embedded_bytes(self, path: Path) -> bytes | None:
        """Extract embedded picture bytes using mutagen (runs in executor)."""
        import asyncio

        def _extract() -> bytes | None:
            from mutagen import File as MutagenFile

            try:
                audio = MutagenFile(str(path))
                if audio is None:
                    return None

                # Try pictures attribute
                if hasattr(audio, "pictures") and audio.pictures:
                    pic = audio.pictures[0]
                    return bytes(pic.data)

                # Try tags directly (mutagen.mp4, mutagen.flac, etc.)
                if hasattr(audio, "tags") and audio.tags:
                    tags = audio.tags
                    # FLAC pictures
                    if hasattr(tags, "pictures") and tags.pictures:
                        pic = tags.pictures[0]
                        return bytes(pic.data)
                    # ID3v2 APIC frames
                    if hasattr(tags, "get"):
                        apic_frames = tags.get("APIC")
                        if apic_frames:
                            if isinstance(apic_frames, list):
                                return bytes(apic_frames[0].data)
                            return bytes(apic_frames.data)
                    # MP4 cover art
                    if hasattr(tags, "covr"):
                        covr = tags.covr
                        if covr:
                            data = covr[0]
                            return bytes(data)

            except Exception:  # noqa: BLE001
                logger.debug("Embedded artwork extraction failed for %s", path)
            return None

        return await asyncio.get_running_loop().run_in_executor(None, _extract)

    async def _process_and_cache(self, source: Path, *, source_name: str) -> ArtworkResult:
        """Process a source image: load, hash, resize to all sizes, and cache."""
        try:
            img = await self._load_image(source)
        except Exception:  # noqa: BLE001
            logger.debug("Could not load image %s", source)
            return ArtworkResult()

        # Compute content hash
        img_bytes = await self._image_to_bytes(img, fmt="JPEG")
        art_hash = hashlib.md5(img_bytes).hexdigest()

        # Resize to all standard sizes
        sizes: dict[tuple[int, int], Path] = {}
        for width, height in ARTWORK_SIZES:
            try:
                resized = img.copy()
                resized.thumbnail((width, height), Image.LANCZOS)
                out_path = self.artwork_cache_dir / f"{art_hash}_{width}x{height}.jpg"
                await self._save_image(resized, out_path)
                sizes[(width, height)] = out_path
            except Exception:  # noqa: BLE001
                logger.debug("Could not resize artwork to %dx%d", width, height)

        cache_path = sizes.get((400, 400)) or sizes.get((200, 200)) or next(iter(sizes.values()), None)

        return ArtworkResult(
            found=True,
            source=source_name,
            cache_path=cache_path,
            sizes=sizes,
            mime_type="image/jpeg",
            width=img.width,
            height=img.height,
            hash=art_hash,
        )

    async def _from_file(self, path: Path, *, source: str) -> ArtworkResult:
        """Create ArtworkResult from a file path."""
        return await self._process_and_cache(path, source_name=source)

    async def _resize_and_cache(
        self,
        source_image: Path,
        target_widths: list[int],
    ) -> dict[tuple[int, int], Path]:
        """Resize a source image to a list of target widths."""
        try:
            img = await self._load_image(source_image)
        except Exception:  # noqa: BLE001
            return {}

        result = {}
        for width in target_widths:
            height = int(img.height * (width / img.width))
            try:
                resized = img.copy()
                resized.thumbnail((width, height), Image.LANCZOS)
                art_hash = hashlib.md5(await self._image_to_bytes(resized)).hexdigest()
                out_path = self.artwork_cache_dir / f"{art_hash}_{width}x{height}.jpg"
                await self._save_image(resized, out_path)
                result[(width, height)] = out_path
            except Exception:  # noqa: BLE001
                logger.debug("Resize to %dx%d failed", width, height)

        return result

    # ---- Image I/O (runs in thread pool) ----

    async def _load_image(self, path: Path) -> Image.Image:
        """Load a PIL Image in a thread pool."""
        loop = asyncio.get_running_loop()

        def _load() -> Image.Image:
            img = Image.open(path)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            return img

        return await loop.run_in_executor(None, _load)

    async def _save_image(self, img: Image.Image, out_path: Path) -> None:
        """Save a PIL Image in a thread pool."""
        loop = asyncio.get_running_loop()

        def _save() -> None:
            img.save(out_path, "JPEG", quality=JPEG_QUALITY, optimize=True)

        await loop.run_in_executor(None, _save)

    async def _image_to_bytes(self, img: Image.Image, fmt: str = "JPEG") -> bytes:
        """Convert PIL Image to bytes."""
        loop = asyncio.get_running_loop()

        def _to_bytes() -> bytes:
            import io
            buf = io.BytesIO()
            img.save(buf, format=fmt, quality=JPEG_QUALITY)
            return buf.getvalue()

        return await loop.run_in_executor(None, _to_bytes)

    # ---- DB cache ----

    async def _db_schema(self) -> None:
        """Ensure the artwork cache DB table exists."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS artwork_cache (
                    cache_key       TEXT PRIMARY KEY,
                    source          TEXT NOT NULL,
                    hash            TEXT NOT NULL,
                    mime_type       TEXT DEFAULT 'image/jpeg',
                    width           INTEGER,
                    height          INTEGER,
                    cached_at       INTEGER DEFAULT (strftime('%s', 'now'))
                )
            """)
            await db.commit()

    async def _load_from_db_cache(
        self,
        track_path: Path | None,
        album: str | None,
        artist: str | None,
    ) -> ArtworkResult | None:
        """Load cached artwork metadata from the DB."""
        await self._db_schema()
        cache_key = self._cache_key(track_path, album, artist)

        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                async with db.execute(
                    "SELECT source, hash, mime_type, width, height FROM artwork_cache WHERE cache_key = ?",
                    (cache_key,),
                ) as cur:
                    row = await cur.fetchone()
        except Exception:  # noqa: BLE001
            return None

        if not row:
            return None

        source, art_hash, mime_type, width, height = row
        # Reconstruct size paths
        sizes: dict[tuple[int, int], Path] = {}
        for w, h in ARTWORK_SIZES:
            p = self.artwork_cache_dir / f"{art_hash}_{w}x{h}.jpg"
            if p.exists():
                sizes[(w, h)] = p

        return ArtworkResult(
            found=True,
            source=source,
            cache_path=sizes.get((400, 400)),
            sizes=sizes,
            mime_type=mime_type or "image/jpeg",
            width=width or 0,
            height=height or 0,
            hash=art_hash or "",
        )

    async def _save_to_db_cache(self, cache_key: str, result: ArtworkResult) -> None:
        """Persist artwork cache metadata to the DB."""
        await self._db_schema()
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO artwork_cache
                    (cache_key, source, hash, mime_type, width, height)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    cache_key,
                    result.source,
                    result.hash,
                    result.mime_type,
                    result.width,
                    result.height,
                ))
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.debug("Could not save artwork cache to DB")
