"""Track, artist, and album metadata models for Pyrion Music Server."""
from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

import aiosqlite

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TrackInfo:
    """
    Metadata for a single audio track.

    Mirrors the data stored in the ``tracks`` database table.
    """

    id: int | None = None
    path: str = ""
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    genre: str | None = None
    year: int | None = None
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    format: str | None = None
    mime_type: str | None = None
    compilation: bool = False
    comment: str | None = None
    mb_track_id: str | None = None
    mb_album_id: str | None = None
    file_size: int = 0
    mtime: int = 0
    embedded_artwork: bool = False
    release_type: str | None = None
    folder_name: str | None = None
    parent_folder: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.duration_ms is None:
            return None
        return self.duration_ms / 1000.0

    @property
    def display_title(self) -> str:
        """Return title or filename stem."""
        if self.title:
            return self.title
        return Path(self.path).stem

    @property
    def effective_artist(self) -> str | None:
        return self.album_artist or self.artist

    @property
    def formatted_track_number(self) -> str:
        """Return track number padded to 2 digits."""
        if self.track_number is None:
            return ""
        return f"{self.track_number:02d}"

    @property
    def path_obj(self) -> Path:
        return Path(self.path)


@dataclass
class ArtistInfo:
    """Metadata for a music artist."""

    id: int | None = None
    name: str = ""
    musicbrainz_id: str | None = None
    sort_name: str | None = None
    track_count: int = 0
    album_count: int = 0

    @property
    def display_name(self) -> str:
        return self.name or "(Unknown Artist)"

    @property
    def sort_key(self) -> str:
        """Sortable name (handles 'The' prefix)."""
        name = self.sort_name or self.name
        return _strip_the_prefix(name)


@dataclass
class AlbumInfo:
    """Metadata for a music album."""

    id: int | None = None
    title: str = ""
    artist: str | None = None
    year: int | None = None
    genre: str | None = None
    compilation: bool = False
    release_type: str | None = None
    track_count: int = 0
    total_duration_ms: int | None = None
    musicbrainz_id: str | None = None
    artwork_hash: str | None = None

    @property
    def display_title(self) -> str:
        return self.title or "(Unknown Album)"

    @property
    def total_duration_seconds(self) -> float | None:
        if self.total_duration_ms is None:
            return None
        return self.total_duration_ms / 1000.0


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _strip_the_prefix(name: str) -> str:
    """Move 'The' prefix to the end for sorting (e.g., 'Beatles, The')."""
    if not name:
        return name
    stripped = name.strip()
    if stripped.lower().startswith("the "):
        return stripped[4:] + ", The"
    return stripped


def normalise_artist_name(name: str) -> str:
    """
    Normalise an artist name for consistent matching.

    - Strips leading/trailing whitespace
    - Collapses multiple spaces
    - Removes accents/diacritics for comparison
    - Moves "The " prefix to end (sort-name form)
    """
    name = name.strip()
    name = " ".join(name.split())
    # Unicode normalisation (NFD) separates accents from letters
    name = unicodedata.normalize("NFD", name)
    # Strip combining diacritical marks
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = unicodedata.normalize("NFC", name)
    return name


# ---------------------------------------------------------------------------
# TrackInfo repository
# ---------------------------------------------------------------------------

class TrackRepository:
    """
    Async repository for track CRUD operations backed by SQLite.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    async def by_id(self, track_id: int) -> TrackInfo | None:
        row = await self._fetchone(
            "SELECT * FROM tracks WHERE id = ?", (track_id,)
        )
        return self._row_to_track(row) if row else None

    async def by_path(self, path: str | Path) -> TrackInfo | None:
        row = await self._fetchone(
            "SELECT * FROM tracks WHERE path = ?", (str(path),)
        )
        return self._row_to_track(row) if row else None

    async def by_album(
        self,
        album: str,
        artist: str | None = None,
    ) -> list[TrackInfo]:
        if artist:
            rows = await self._fetchall(
                "SELECT * FROM tracks WHERE album = ? AND (artist = ? OR album_artist = ?) ORDER BY disc_number, track_number",
                (album, artist, artist),
            )
        else:
            rows = await self._fetchall(
                "SELECT * FROM tracks WHERE album = ? ORDER BY disc_number, track_number",
                (album,),
            )
        return [self._row_to_track(r) for r in rows if r]

    async def by_artist(self, artist: str) -> list[TrackInfo]:
        rows = await self._fetchall(
            "SELECT * FROM tracks WHERE artist = ? OR album_artist = ? ORDER BY album, disc_number, track_number",
            (artist, artist),
        )
        return [self._row_to_track(r) for r in rows if r]

    async def search(
        self,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TrackInfo]:
        pattern = f"%{query}%"
        rows = await self._fetchall(
            """
            SELECT * FROM tracks
            WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
            ORDER BY artist, album, disc_number, track_number
            LIMIT ? OFFSET ?
            """,
            (pattern, pattern, pattern, limit, offset),
        )
        return [self._row_to_track(r) for r in rows if r]

    async def count(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) as n FROM tracks")
        return row["n"] if row else 0

    async def all_artists(self) -> list[str]:
        """Return sorted unique artist names from all tracks."""
        rows = await self._fetchall(
            "SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL AND artist != '' ORDER BY artist"
        )
        return [r["artist"] for r in rows if r["artist"]]

    async def all_albums(self) -> list[AlbumInfo]:
        """Return distinct album metadata aggregated from tracks."""
        rows = await self._fetchall("""
            SELECT
                album,
                MAX(album_artist) as artist,
                MAX(year) as year,
                MAX(genre) as genre,
                MAX(compilation) as compilation,
                MAX(release_type) as release_type,
                COUNT(*) as track_count,
                SUM(duration_ms) as total_duration_ms,
                MAX(mb_album_id) as musicbrainz_id,
                MAX(artwork_hash) as artwork_hash
            FROM tracks
            WHERE album IS NOT NULL AND album != ''
            GROUP BY album
            ORDER BY MAX(album_artist), album
        """)
        albums = []
        for r in rows:
            if not r["album"]:
                continue
            albums.append(AlbumInfo(
                id=None,
                title=r["album"],
                artist=r["artist"],
                year=r["year"],
                genre=r["genre"],
                compilation=bool(r["compilation"]),
                release_type=r["release_type"],
                track_count=r["track_count"] or 0,
                total_duration_ms=r["total_duration_ms"],
                musicbrainz_id=r["musicbrainz_id"],
                artwork_hash=r["artwork_hash"],
            ))
        return albums

    # ---- internal ----

    async def _fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(sql, params) as cur:
                    row = await cur.fetchone()
                    return dict(row) if row else None
        except Exception:  # noqa: BLE001
            logger.exception("DB query error in _fetchone")
            return None

    async def _fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(sql, params) as cur:
                    rows = await cur.fetchall()
                    return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            logger.exception("DB query error in _fetchall")
            return []

    @staticmethod
    def _row_to_track(row: dict[str, Any]) -> TrackInfo:
        return TrackInfo(
            id=row.get("id"),
            path=str(row.get("path", "")),
            title=row.get("title"),
            artist=row.get("artist"),
            album=row.get("album"),
            album_artist=row.get("album_artist"),
            genre=row.get("genre"),
            year=row.get("year"),
            track_number=row.get("track_number"),
            disc_number=row.get("disc_number"),
            duration_ms=row.get("duration_ms"),
            bitrate=row.get("bitrate"),
            sample_rate=row.get("sample_rate"),
            channels=row.get("channels"),
            format=row.get("format"),
            mime_type=row.get("mime_type"),
            compilation=bool(row.get("compilation")),
            comment=row.get("comment"),
            mb_track_id=row.get("mb_track_id"),
            mb_album_id=row.get("mb_album_id"),
            file_size=row.get("file_size", 0),
            mtime=row.get("mtime", 0),
            embedded_artwork=bool(row.get("embedded_artwork")),
            release_type=row.get("release_type"),
            folder_name=row.get("folder_name"),
            parent_folder=row.get("parent_folder"),
        )
