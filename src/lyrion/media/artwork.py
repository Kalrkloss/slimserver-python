"""Artwork extraction, discovery, resizing, and caching for Pyrion Music Server."""
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


#: Padding colour Perl paints behind a fitted image.
#: ``Slim/Web/Graphics.pm:227-234`` passes **no** ``bgcolor`` into
#: ``GDResizer->resize``, ``Slim/Utils/GDResizer.pm:41`` therefore leaves it
#: undef, its ``:104-106`` fixup only rewrites a non-empty wrong-length value,
#: and ``:169`` hands ``hex(undef)`` — i.e. ``0`` — to Image::Scale.
#: Live Perl 9.1.1 (read-only, 2026-09-19, ``/imageproxy/<160x85 logo>/<spec>``):
#: every padded corner pixel is exactly ``(0, 0, 0, 255)`` — opaque black.
PAD_RGB: tuple[int, int, int] = (0, 0, 0)


def gd_effective_box(
    in_w: int,
    in_h: int,
    req_w: int,
    req_h: int,
) -> tuple[int, int]:
    """Perl ``Slim/Utils/GDResizer.pm:152-162`` — box actually resized into.

    "requested size is larger than original - don't upscale": only when the
    request is bigger in **both** dimensions is it pulled back to the
    original's own long edge::

        if ( $width > $in_width && $height > $in_height ) {
            if ( $in_height / $in_width > 1 ) {   # portrait
                $height = $height * ($in_width / $width);
                $width  = $in_width;
            } else {
                $width  = $width * ($in_height / $height);
                $height = $in_height;
            }
        }

    The result is *not* the original size: a 160x85 logo asked for 200x200
    comes back as a 85x85 box (live 9.1.1: ``/imageproxy/<160x85>/200x200_m``
    -> 85x85 padded PNG; ``300x300_m`` -> the same 85x85), and a 40x40 source
    asked for 200x200 comes back as 40x40.
    """
    if req_w and req_h and req_w > in_w and req_h > in_h:
        if in_h / in_w > 1:
            return in_w, max(1, int(round(req_h * (in_w / req_w))))
        return max(1, int(round(req_w * (in_h / req_h)))), in_h
    return req_w, req_h


def fit_and_pad(
    img: "Image.Image",
    box_w: int,
    box_h: int,
    rgb: tuple[int, int, int] = PAD_RGB,
) -> "Image.Image":
    """Fit ``img`` into the box, keep the aspect, pad the rest — Perl's
    ``$im->resize({width, height, bgcolor => hex($bgcolor), keep_aspect => 1})``
    (``Slim/Utils/GDResizer.pm:166-171``).

    ``Image::Scale`` fits the image into the requested box, centres it and
    fills the remaining border with the background colour.  Live 9.1.1 for a
    160x85 logo at ``40x40_m``: box 40x40, content 40x21, 9 black rows on top
    and 10 below — i.e. **floor** centring, which is what this computes.
    """
    if box_w <= 0 or box_h <= 0:
        return img
    scale = min(box_w / img.width, box_h / img.height)
    cw = max(1, min(box_w, int(round(img.width * scale))))
    ch = max(1, min(box_h, int(round(img.height * scale))))
    if (cw, ch) == (img.width, img.height) and (cw, ch) == (box_w, box_h):
        return img                             # nothing to do at all
    resample = getattr(
        getattr(Image, "Resampling", Image), "LANCZOS", None
    ) or getattr(Image, "LANCZOS", 1)
    content = img if (cw, ch) == (img.width, img.height) else \
        img.resize((cw, ch), resample)
    if (cw, ch) == (box_w, box_h):
        return content
    canvas = Image.new("RGB", (box_w, box_h), rgb)
    canvas.paste(content.convert("RGB"), ((box_w - cw) // 2, (box_h - ch) // 2))
    return canvas


# ---------------------------------------------------------------------------
# Eingebettetes Tag-Bild (Perl ``_readCoverArtTags``)
# ---------------------------------------------------------------------------

#: Unterordner des Server-Caches, in den ein Tag-Bild materialisiert wird.
#: Perl hält seinen Artwork-Cache bei den Bibliotheksdaten
#: (``Slim/Utils/ArtworkCache.pm:44-47``: ``librarycachedir``); hier ist das
#: ``<serverdata>/cache/artwork/embedded``.
EMBEDDED_ARTWORK_SUBDIR: tuple[str, ...] = ("artwork", "embedded")

#: Magic Bytes → Dateiendung.  Die Endung bestimmt den Content-Type der
#: Auslieferung (``web/app.py`` ``_MIME_BY_EXT``); Perl erkennt den Typ ebenso
#: an den Magic Bytes (``Slim/Music/Artwork.pm:437-478`` ``_imageContentType``,
#: ``:381-385`` ``_readCoverArtImage`` gibt ``($content, $contentType)``).
_IMAGE_EXTENSIONS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


def image_extension(data: bytes) -> str:
    """Dateiendung aus den Magic Bytes (Fallback ``.jpg``).

    Ein unbekanntes Format wird nicht verworfen: Perl gibt die Tag-Bytes
    unverändert zurück (``Slim/Formats/MP3.pm:177-180``), und der
    Auslieferungspfad nimmt bei unbekannter Endung ``image/jpeg`` an
    (``web/app.py`` ``_MIME_BY_EXT``).
    """
    if not data:
        return ".jpg"
    for magic, ext in _IMAGE_EXTENSIONS:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def default_embedded_artwork_dir() -> Path:
    """Wohin materialisierte Tag-Bilder geschrieben werden.

    ``<serverdata>/cache/artwork/embedded`` (Perl: Artwork-Cache bei den
    Bibliotheksdaten, ``Slim/Utils/ArtworkCache.pm:44-47``); ohne initialisierte
    Konfiguration (Einzelskript, Tests) ``~/.lyrion/cache/artwork/embedded``.
    """
    try:
        from lyrion.config import get_config

        return Path(get_config().cache_dir).joinpath(*EMBEDDED_ARTWORK_SUBDIR)
    except Exception:  # noqa: BLE001 - Start ohne Config ist erlaubt
        return Path.home() / ".lyrion" / "cache" / "artwork" / "embedded"


def _picture_data(picture: Any) -> bytes | None:
    """Bilddaten eines mutagen-Bildobjekts (``Picture.data``) bzw. eines Dicts."""
    if picture is None:
        return None
    data = getattr(picture, "data", None)
    if data is None and isinstance(picture, dict):
        data = picture.get("data")
    if data is None:
        return None
    if isinstance(data, str):  # notfalls latin-1-Rohbytes
        data = data.encode("latin-1", "replace")
    try:
        return bytes(data)
    except (TypeError, ValueError):
        return None


def embedded_picture_bytes(audio_file: Any, tags: Any = None) -> bytes | None:
    """Erstes eingebettetes Bild eines **geöffneten** mutagen-Objekts.

    Perl ``Slim/Music/Artwork.pm:495-505`` (``_readCoverArtTags``) ruft dafür
    ``getCoverArt`` der Formatsklasse auf; ``Slim/Formats/MP3.pm:159-181``
    liest das ID3-``APIC`` und nimmt bei mehreren Bildern das mit dem
    **kleinsten** ``image_type``.  Reihenfolge wie
    :meth:`ArtworkHandler._extract_embedded_bytes`: FLAC/OGG ``pictures`` →
    ID3 ``APIC`` → MP4 ``covr`` → die übrigen Bild-Tags (Vorbis
    ``METADATA_BLOCK_PICTURE``, APE ``Cover Art (Front)``, ASF ``WM/Picture``).
    Nimmt ein Format sein Bild nicht über diese Felder heraus, bleibt es bei
    ``None`` — die *Tatsache* eines Tag-Bildes erkennt weiterhin
    ``scanner._has_embedded_artwork``, die Online-Suche bleibt also aus.
    """
    if tags is None:
        tags = getattr(audio_file, "tags", None)
    try:
        for container in (audio_file, tags):
            if container is None:
                continue
            pictures = getattr(container, "pictures", None)
            if pictures:
                data = _picture_data(pictures[0])
                if data:
                    return data

        if tags is None:
            return None

        getall = getattr(tags, "getall", None)          # ID3 kann mehrere APIC führen
        if callable(getall):
            found: Any = getall("APIC")
            frames: list[Any] = list(found) if found else []
            if frames:
                # Perl ``Slim/Formats/MP3.pm:174-176``: bei mehreren Bildern das
                # mit dem kleinsten ``image_type``.
                frames.sort(key=lambda f: int(getattr(f, "type", 0) or 0))
                data = _picture_data(frames[0])
                if data:
                    return data

        apic = tags.get("APIC")
        if apic:
            data = _picture_data(apic[0] if isinstance(apic, (list, tuple)) else apic)
            if data:
                return data

        covr = tags.get("covr")                          # MP4/M4A
        if covr:
            data = _picture_data(covr[0] if isinstance(covr, (list, tuple)) else covr)
            if data:
                return data

        for key in ("metadata_block_picture", "METADATA_BLOCK_PICTURE",
                    "Cover Art (Front)", "cover art (front)", "WM/Picture"):
            sidecar = tags.get(key)
            if not sidecar:
                continue
            data = _picture_data(sidecar[0] if isinstance(sidecar, (list, tuple))
                                 else sidecar)
            if data:
                return data
    except Exception:  # noqa: BLE001 - ein kaputter Tag-Container ist kein Fehler
        logger.debug("Tag-Bild nicht lesbar aus %s", audio_file)
    return None


def write_embedded_artwork(data: bytes, cache_dir: Path | None = None) -> Path | None:
    """Tag-Bild als Datei im Artwork-Cache ablegen — Pfad zurück.

    Der Dateiname ist der Inhalts-Hash (``<sha1[:20]><endung>``): dasselbe Bild
    wird nie doppelt geschrieben, und ein erneuter Scan findet dieselbe Datei
    wieder — Perls ``generateImageId`` bildet den Cover-Schlüssel ebenfalls aus
    dem Bild (``Slim/Music/Artwork.pm:404-440``, ``Slim/Schema.pm:1819-1826``)
    und der Cache liegt dauerhaft auf Platte (``Slim/Utils/ArtworkCache.pm:44-66``).
    Geschrieben wird atomar (``.part`` → ``replace``), damit ein abgebrochener
    Scan keine halbe Bilddatei hinterlässt.  ``None`` = keine Bilddaten.
    """
    if not data:
        return None
    directory = Path(cache_dir) if cache_dir is not None else default_embedded_artwork_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{hashlib.sha1(data).hexdigest()[:20]}{image_extension(data)}"
    if not target.is_file() or target.stat().st_size != len(data):
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
    return target


async def materialize_embedded_artwork(
    track_path: Path,
    cache_dir: Path | None = None,
) -> Path | None:
    """Tag-Bild einer Datei materialisieren (eigener mutagen-Lauf).

    Für Aufrufer ohne bereits geöffnetes mutagen-Objekt.  Der Scanner nutzt
    :func:`embedded_picture_bytes` + :func:`write_embedded_artwork` auf dem schon
    geöffneten Objekt und spart sich den zweiten Parse.
    """
    loop = asyncio.get_running_loop()

    def _work() -> Path | None:
        from mutagen import File as MutagenFile

        try:
            audio = MutagenFile(str(track_path))
        except Exception:  # noqa: BLE001
            logger.debug("Tag-Bild: %s nicht lesbar", track_path)
            return None
        if audio is None:
            return None
        try:
            data = embedded_picture_bytes(audio)
        finally:
            try:
                audio.close()
            except Exception:  # noqa: BLE001
                pass
        return write_embedded_artwork(data, cache_dir) if data else None

    return await loop.run_in_executor(None, _work)



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

        # 1. Try embedded (Perl ``_readCoverArtTags``, ``Slim/Music/Artwork.pm:480-516``).
        embedded_bytes = await self._extract_embedded_bytes(track_path)
        if embedded_bytes:
            loop = asyncio.get_running_loop()
            source = await loop.run_in_executor(
                None, write_embedded_artwork, embedded_bytes, self.artwork_cache_dir)
            if source is not None:
                return await self._process_and_cache(source, source_name="embedded")

        # 2. Try folder scan (Perl ``_readCoverArtFiles``, ``:517-637``).
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

        # Resize to all standard sizes.  Perl's box, not just a fit: a
        # non-square cover is padded to the box (``GDResizer.pm:166-171``),
        # so the cached file really is ``<w>x<h>`` — the key below already
        # claimed that, the bytes did not.
        sizes: dict[tuple[int, int], Path] = {}
        for width, height in ARTWORK_SIZES:
            try:
                box_w, box_h = gd_effective_box(
                    img.width, img.height, width, height)
                resized = fit_and_pad(img, box_w, box_h)
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
        """Resize a source image into square boxes of the given widths.

        The box is the *requested* size, padded like Perl's ``mode 'm'``
        (``GDResizer.pm:140-171``): Perl's ``getResizeSpecs``
        (``Slim/Music/Artwork.pm:864-870``) asks for ``64x64_m`` / ``41x41_m``
        / ``40x40_m`` and a non-square logo comes back as exactly that box.
        The key is the padded box, so callers can trust ``(w, h)`` again.
        """
        try:
            img = await self._load_image(source_image)
        except Exception:  # noqa: BLE001
            return {}

        result = {}
        for width in target_widths:
            try:
                box_w, box_h = gd_effective_box(img.width, img.height, width, width)
                resized = fit_and_pad(img, box_w, box_h)
                art_hash = hashlib.md5(await self._image_to_bytes(resized)).hexdigest()
                out_path = self.artwork_cache_dir / f"{art_hash}_{width}x{width}.jpg"
                await self._save_image(resized, out_path)
                result[(width, width)] = out_path
            except Exception:  # noqa: BLE001
                logger.debug("Resize to %dx%d failed", width, width)

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
