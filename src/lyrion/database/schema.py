"""
SQLAlchemy ORM models for the Pyrion Music Server database schema.

This schema is ported from the Perl LMS file Slim/Schema.pm, which defines
~3000 lines of SQLite CREATE TABLE statements. The Python models use
SQLAlchemy 2.0-style declarative bases with async support.

Main entities:
- Track: individual audio file
- Album: collection of tracks
- Contributor: artist, composer, conductor, etc.
- Genre: music genre
- Year: year table for tracks
- Playlist: named playlist
- PlaylistItem: item within a playlist
- TracksPersistent: persistent track metadata
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    Boolean,
    Float,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    CheckConstraint,
    Enum as SQLEnum,
    Table,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    backref,
    mapped_column,
    relationship,
    Session,
)
from sqlalchemy.dialects.sqlite import JSON as SQLJSON

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""

    type_annotation_map = {
        dict[str, Any]: SQLJSON,
    }


# ---------------------------------------------------------------------------
# Association / junction tables
# ---------------------------------------------------------------------------

tracks_contributors = Table(
    "tracks_contributors",
    Base.metadata,
    Column("track", Integer, ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True),
    Column("contributor", Integer, ForeignKey("contributors.id", ondelete="CASCADE"), primary_key=True),
    Column("role", Integer, nullable=False, default=1),  # 1=artist, 2=composer, 3=conductor...
    Index("idx_tc_track", "track"),
    Index("idx_tc_contributor", "contributor"),
    Index("idx_tc_role", "role"),
)

tracks_genres = Table(
    "tracks_genres",
    Base.metadata,
    Column("track", Integer, ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True),
    Column("genre", Integer, ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
    Index("idx_tg_track", "track"),
    Index("idx_tg_genre", "genre"),
)

tracks_albums = Table(
    "tracks_albums",
    Base.metadata,
    Column("track", Integer, ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True),
    Column("album", Integer, ForeignKey("albums.id", ondelete="CASCADE"), primary_key=True),
    Column("position", Integer, nullable=False, default=0),
    Index("idx_ta_track", "track"),
    Index("idx_ta_album", "album"),
)

albums_contributors = Table(
    "albums_contributors",
    Base.metadata,
    Column("album", Integer, ForeignKey("albums.id", ondelete="CASCADE"), primary_key=True),
    Column("contributor", Integer, ForeignKey("contributors.id", ondelete="CASCADE"), primary_key=True),
    Column("role", Integer, nullable=False, default=1),
    Index("idx_ac_album", "album"),
    Index("idx_ac_contributor", "contributor"),
)


# ---------------------------------------------------------------------------
# Genre
# ---------------------------------------------------------------------------

class Genre(Base):
    """
    Music genre entity.

    Genres are stored hierarchically (parent_id) and have a namespell
    for fast lookup.
    """

    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespell: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    parent: Mapped[int | None] = mapped_column(Integer, ForeignKey("genres.id", ondelete="SET NULL"), nullable=True)
    sortkey: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    parent_genre: Mapped[Genre | None] = relationship("Genre", remote_side=[id], back_populates="subgenres")
    subgenres: Mapped[list[Genre]] = relationship("Genre", back_populates="parent_genre", cascade="all")
    tracks: Mapped[list[Track]] = relationship(secondary=tracks_genres, back_populates="genres")

    __table_args__ = (
        UniqueConstraint("namespell", name="uq_genre_namespell"),
        Index("idx_genre_parent", "parent"),
    )

    def __repr__(self) -> str:
        return f"<Genre(id={self.id}, name={self.namespell!r})>"


# ---------------------------------------------------------------------------
# Contributor
# ---------------------------------------------------------------------------

#: Contributor role constants (matching LMS values)
CONTRIB_ROLE_ARTIST = 1
CONTRIB_ROLE_COMPOSER = 2
CONTRIB_ROLE_CONDUCTOR = 3
CONTRIB_ROLE_BAND = 4
CONTRIB_ROLE_ALBUMARTIST = 5
CONTRIB_ROLE_TRACKARTIST = 6
CONTRIB_ROLE_REMIXER = 7
CONTRIB_ROLE_DISCID = 99


class Contributor(Base):
    """
    Contributor (artist, composer, conductor, etc.).

    Contributors have a namespell (lowercase, normalized) used for
    fast lookups and deduplication.
    """

    __tablename__ = "contributors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespell: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    sortname: Mapped[str | None] = mapped_column(String(255), nullable=True)  # sort name / last name
    customsearch: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    musicbrainz_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Remote metadata
    artflow_flag: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    tracks: Mapped[list[Track]] = relationship(
        secondary=tracks_contributors, back_populates="contributors"
    )
    albums: Mapped[list[Album]] = relationship(
        secondary=albums_contributors, back_populates="contributors"
    )

    __table_args__ = (
        UniqueConstraint("namespell", name="uq_contributor_namespell"),
        Index("idx_contrib_name", "name"),
        Index("idx_contrib_musicbrainz", "musicbrainz_id"),
    )

    @property
    def display_name(self) -> str:
        return self.name or self.namespell

    def __repr__(self) -> str:
        return f"<Contributor(id={self.id}, name={self.name!r})>"


# ---------------------------------------------------------------------------
# Album
# ---------------------------------------------------------------------------

class Album(Base):
    """
    Music album.

    An album groups one or more tracks. It has metadata like title,
    year, and artwork. The album can have multiple contributors (artists).
    """

    __tablename__ = "albums"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    titlesort: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    disccount: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    disc: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compilation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # boolean-ish
    musicbrainz_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    artwork: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    artwork_front: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Counters (denormalized for performance)
    numtracks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    numdiscs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Remote metadata
    artflow_flag: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Exclude flags
    disabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Misc
    remixer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mood: Mapped[str | None] = mapped_column(String(255), nullable=True)
    style: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    samplerate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format: Mapped[str | None] = mapped_column(String(50), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    contributors: Mapped[list[Contributor]] = relationship(
        secondary=albums_contributors, back_populates="albums"
    )
    tracks: Mapped[list[Track]] = relationship(
        secondary=tracks_albums, back_populates="albums",
        cascade="all",
    )

    __table_args__ = (
        UniqueConstraint("titlesort", "year", name="uq_album_titlesort_year"),
        Index("idx_album_year", "year"),
        Index("idx_album_musicbrainz", "musicbrainz_id"),
        Index("idx_album_compilation", "compilation"),
        CheckConstraint("disc >= 0", name="ck_album_disc"),
        CheckConstraint("disccount >= 0", name="ck_album_disccount"),
    )

    @property
    def display_title(self) -> str:
        return self.title or self.titlesort or "Unknown Album"

    def __repr__(self) -> str:
        return f"<Album(id={self.id}, title={self.title!r})>"


# ---------------------------------------------------------------------------
# Track
# ---------------------------------------------------------------------------

class Track(Base):
    """
    Individual audio track.

    A track represents a single audio file (or stream URL) with all
    associated metadata: title, artist, album, genre, year, duration,
    bitrate, file path, etc.
    """

    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    titlesort: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # File metadata
    url: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True, index=True)
    content_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    modtime: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    filesize: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Audio properties
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    samplerate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bitspersample: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_alignment: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bitrate_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Playback info
    duration: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    playcount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastplayed: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lastscanned: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=datetime.utcnow)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Deduplication
    musicbrainz_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    musicdns_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    discid: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Remote artwork
    artwork: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    artwork_front: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    remote: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Classification
    audio: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    video: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Exclude flags
    disabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Lyrics
    lyrics: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Replay gain
    replay_gain: Mapped[float | None] = mapped_column(Float, nullable=True)
    replay_peak: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Misc
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    mood: Mapped[str | None] = mapped_column(String(255), nullable=True)
    style: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    genre: Mapped[str | None] = mapped_column(String(255), nullable=True)
    type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Cover data (embedded artwork as blob)
    cover: Mapped[bytes | None] = mapped_column(Text, nullable=True)  # base64-encoded in Perl
    # Track numbering
    tracknum: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Additional metadata
    compilation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    composer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conductor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    orchestra: Mapped[str | None] = mapped_column(String(255), nullable=True)
    band: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Remote metadata
    artflow_flag: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    contributors: Mapped[list[Contributor]] = relationship(
        secondary=tracks_contributors, back_populates="tracks"
    )
    genres: Mapped[list[Genre]] = relationship(
        secondary=tracks_genres, back_populates="tracks"
    )
    albums: Mapped[list[Album]] = relationship(
        secondary=tracks_albums, back_populates="tracks"
    )

    __table_args__ = (
        Index("idx_track_title", "title"),
        Index("idx_track_year", "year"),
        Index("idx_track_musicbrainz", "musicbrainz_id"),
        Index("idx_track_musicdns", "musicdns_id"),
        Index("idx_track_discid", "discid"),
        Index("idx_track_lastplayed", "lastplayed"),
        Index("idx_track_lastscanned", "lastscanned"),
        Index("idx_track_remote", "remote"),
        Index("idx_track_disabled", "disabled"),
        CheckConstraint("duration >= 0", name="ck_track_duration"),
        CheckConstraint("playcount >= 0", name="ck_track_playcount"),
    )

    @property
    def display_title(self) -> str:
        return self.title or self.titlesort or self.url.split("/")[-1]

    @property
    def is_audio(self) -> bool:
        return bool(self.audio)

    @property
    def is_video(self) -> bool:
        return bool(self.video)

    def __repr__(self) -> str:
        return f"<Track(id={self.id}, title={self.title!r}, url={self.url[:40]!r}...)>"


# ---------------------------------------------------------------------------
# Year
# ---------------------------------------------------------------------------

class Year(Base):
    """Year lookup table for track year values."""

    __tablename__ = "years"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)

    __table_args__ = (
        CheckConstraint("year >= 0", name="ck_year_positive"),
    )

    def __repr__(self) -> str:
        return f"<Year({self.year})>"


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------

class Playlist(Base):
    """
    Named playlist (favorites, saved playlist, dynamic playlist, etc.).
    """

    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    playlist: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    changed: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    # Type: 0=saved playlist, 1=favorites, 2=dynamic
    pl_type: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Dynamic playlist parameters (stored as JSON)
    dynamic_parameters: Mapped[dict[str, Any] | None] = mapped_column(SQLJSON, nullable=True)
    # Remote playlist
    remote: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remote_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Exclude
    disabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    items: Mapped[list[PlaylistItem]] = relationship(
        "PlaylistItem",
        back_populates="playlist_ref",
        cascade="all, delete-orphan",
        order_by="PlaylistItem.position",
    )

    __table_args__ = (
        UniqueConstraint("playlist", name="uq_playlist_name"),
        Index("idx_playlist_changed", "changed"),
        Index("idx_playlist_type", "pl_type"),
    )

    @property
    def display_name(self) -> str:
        return self.name or self.playlist

    def __repr__(self) -> str:
        return f"<Playlist(id={self.id}, name={self.name!r})>"


class PlaylistItem(Base):
    """
    Individual item within a playlist.

    References a track (or external URL) with an ordering position.
    """

    __tablename__ = "playlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    playlist: Mapped[int] = mapped_column(Integer, ForeignKey("playlists.id", ondelete="CASCADE"), nullable=False)
    track: Mapped[int | None] = mapped_column(Integer, ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Non-track metadata
    added: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    # Snapshot of track metadata (frozen at playlist creation time)
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(SQLJSON, nullable=True)

    playlist_ref: Mapped[Playlist] = relationship("Playlist", back_populates="items")
    track_ref: Mapped[Track | None] = relationship("Track")

    __table_args__ = (
        Index("idx_pi_playlist_position", "playlist", "position"),
        Index("idx_pi_track", "track"),
        CheckConstraint("position >= 0", name="ck_pi_position"),
    )

    def __repr__(self) -> str:
        return f"<PlaylistItem(id={self.id}, pos={self.position})>"


# ---------------------------------------------------------------------------
# TracksPersistent
# ---------------------------------------------------------------------------

class TracksPersistent(Base):
    """
    Persistent per-track data (play counts, ratings, last played, etc.).

    This table stores user-specific or semi-stable data about tracks
    that survives library rescans.
    """

    __tablename__ = "tracks_persistent"

    tracks: Mapped[int] = mapped_column(Integer, ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), nullable=False, index=True)
    playcount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastplayed: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lastskipped: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-5 stars
    added: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    played_samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Exclude flags (for dynamic playlists / smart mixes)
    skip_artist: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skip_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Remote metadata
    remote: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remote_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Ephemeral metadata
    dynamic_playlist_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Audio signature for quick content-match
    audio_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)

    track: Mapped[Track | None] = relationship("Track")

    __table_args__ = (
        Index("idx_tp_url", "url"),
        Index("idx_tp_lastplayed", "lastplayed"),
        Index("idx_tp_playcount", "playcount"),
        Index("idx_tp_rating", "rating"),
        Index("idx_tp_remote", "remote"),
    )

    def __repr__(self) -> str:
        return f"<TracksPersistent(track={self.tracks}, playcount={self.playcount})>"


# ---------------------------------------------------------------------------
# Additional LMS tables (abbreviated)
# ---------------------------------------------------------------------------

# These are less central but still important in LMS; providing stubs

class RemoteMedia(Base):
    """
    Remote streaming media (internet radio, streaming services).
    """

    __tablename__ = "remote_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    genre: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artwork_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    stream_format: Mapped[str | None] = mapped_column(String(50), nullable=True)
    lastcheck: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lastplay: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    playcount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_rm_url", "url"),
        Index("idx_rm_lastcheck", "lastcheck"),
    )


class SyncState(Base):
    """
    Synchronized player state snapshot (for multi-player sync groups).
    """

    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_master: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    sync_slave: Mapped[int] = mapped_column(Integer, nullable=False)
    sync_group: Mapped[int] = mapped_column(Integer, nullable=False)
    updated: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("sync_master", "sync_slave", name="uq_sync_pair"),
        Index("idx_sync_master", "sync_master"),
        Index("idx_sync_group", "sync_group"),
    )


class Player(Base):
    """
    Player (Squeezebox / compatible) known to the server.
    """

    __tablename__ = "players"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # MAC address
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)  # IPv6 compatible
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    firmware: Mapped[str | None] = mapped_column(String(100), nullable=True)
    uuid: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lastseen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lastop: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    # Player-specific prefs
    displaytype: Mapped[str | None] = mapped_column(String(100), nullable=True)
    digital_volume_control: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    eq_bands: Mapped[dict[str, Any] | None] = mapped_column(SQLJSON, nullable=True)
    # Player capability flags (bitmap)
    can_wifi: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    can_reconnect: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_squeezelite: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Binary blob for player state
    state_blob: Mapped[bytes | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_player_uuid", "uuid"),
        Index("idx_player_enabled", "enabled"),
        Index("idx_player_lastseen", "lastseen"),
    )


# ---------------------------------------------------------------------------
# Favorites (radio streams & folders, like LMS "Favorites" OPML)
# ---------------------------------------------------------------------------

class Favorite(Base):
    """
    A favorite: either a folder (url IS NULL) or a stream (url set).

    Mirrors the original LMS Favorites structure (OPML with nested
    outlines): folders may contain streams and further sub-folders.
    """

    __tablename__ = "favorites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("favorites.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    children: Mapped[list["Favorite"]] = relationship(
        "Favorite",
        backref=backref("parent", remote_side="Favorite.id"),
        cascade="all, delete-orphan",
        order_by="Favorite.position",
    )

    __table_args__ = (
        Index("idx_fav_parent", "parent_id"),
        Index("idx_fav_parent_position", "parent_id", "position"),
    )

    def __repr__(self) -> str:
        kind = "folder" if self.url is None else "stream"
        return f"<Favorite(id={self.id}, {kind}, {self.title!r})>"


# ---------------------------------------------------------------------------
# Schema metadata helpers
# ---------------------------------------------------------------------------

def create_all(engine: Any) -> None:
    """Create all tables in the database."""
    Base.metadata.create_all(engine)


def drop_all(engine: Any) -> None:
    """Drop all tables from the database (dangerous!)."""
    Base.metadata.drop_all(engine)


def get_table_names(engine: Any) -> list[str]:
    """Return all table names in the schema."""
    from sqlalchemy import inspect
    return inspect(engine).get_table_names()
