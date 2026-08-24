"""
Pyrion Music Server database module.

Provides SQLite-backed persistence using SQLAlchemy ORM and aiosqlite.
The database schema is ported from Slim/Schema.pm.
"""

from lyrion.database.schema import (
    Base,
    Track,
    Album,
    Contributor,
    Genre,
    Year,
    Playlist,
    PlaylistItem,
    TracksPersistent,
)
from lyrion.database.sqlite_helper import (
    init_db,
    close_db,
    get_engine,
    get_session_factory,
    db_session,
    db_readonly_session,
    raw_connection,
)
from lyrion.database.dbcache import (
    DbCache,
    dbcache,
)

__all__ = [
    "Base",
    "Track",
    "Album",
    "Contributor",
    "Genre",
    "Year",
    "Playlist",
    "PlaylistItem",
    "TracksPersistent",
    "init_db",
    "close_db",
    "get_engine",
    "get_session_factory",
    "db_session",
    "db_readonly_session",
    "raw_connection",
    "DbCache",
    "dbcache",
]
