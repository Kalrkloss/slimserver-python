"""Virtual Libraries for Pyrion Music Server."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from lyrion.music.info import TrackInfo, TrackRepository

logger = logging.getLogger(__name__)


@dataclass
class VirtualLibrary:
    """
    A virtual library is a named saved-search filter over the music library.

    It maps to a set of tag-based filter rules stored in the database,
    allowing users to create scoped views (e.g., "Jazz only", "90s music",
    "Lossless files") without physically moving files.

    Attributes
    ----------
    id : int | None
        Database primary key (None for unsaved libraries).
    name : str
        Human-readable library name (e.g., "All Jazz", "My Favourites").
    description : str
        Optional description shown in the UI.
    rules : list[LibraryRule]
        Filter rules that a track must match to belong to this library.
    match_all : bool
        If True, ALL rules must match (AND). If False, ANY rule (OR).
    created_at : int
        Unix timestamp of creation.
    updated_at : int
        Unix timestamp of last modification.
    """

    id: int | None = None
    name: str = ""
    description: str = ""
    rules: list[LibraryRule] = field(default_factory=list)
    match_all: bool = True  # AND by default
    created_at: int = 0
    updated_at: int = 0

    def matches(self, track: TrackInfo) -> bool:
        """
        Return True if a track matches this virtual library's rules.

        Parameters
        ----------
        track
            The track to test.

        Returns
        -------
        bool
        """
        if not self.rules:
            return True  # Empty rule set = match all

        results = [rule.matches(track) for rule in self.rules]

        if self.match_all:
            return all(results)
        return any(results)

    def build_where_clause(self) -> tuple[str, list[Any]]:
        """
        Build a SQL WHERE clause that matches this virtual library's rules.

        Returns
        -------
        (sql_fragment, params)
            A SQL fragment like ``"genre = ? AND year >= ?"`` suitable for
            use in a larger SELECT query, plus the bound parameter list.
        """
        clauses: list[str] = []
        params: list[Any] = []

        for rule in self.rules:
            clause, vals = rule.build_sql()
            if clause:
                clauses.append(f"({clause})")
                params.extend(vals)

        if not clauses:
            return ("1=1", [])

        joiner = " AND " if self.match_all else " OR "
        return (joiner.join(clauses), params)

    def build_where_clause_for_tracks(self) -> tuple[str, list[Any]]:
        """
        Build a SQL WHERE clause for the ``tracks`` table that matches all rules.

        Works the same as :meth:`build_where_clause` but always uses AND
        between individual field clauses for the tracks table.
        """
        clauses, params = self.build_where_clause()
        return clauses, params


@dataclass
class LibraryRule:
    """
    A single filter rule within a virtual library.

    Mirrors the rule structure used by LMS for saved library searches.
    """

    field: str
    operator: str
    value: str | int | list[str]
    negate: bool = False

    def matches(self, track: TrackInfo) -> bool:
        """
        Return True if a track matches this rule.

        Parameters
        ----------
        track
            The track to test.

        Returns
        -------
        bool
        """
        track_val = self._get_track_field(track)
        result = self._apply_operator(track_val)
        return not result if self.negate else result

    def build_sql(self) -> tuple[str, list[Any]]:
        """
        Return a SQL WHERE clause fragment for this rule.

        Returns
        -------
        (clause, params)
            e.g. ``("(genre = ?)", ["Jazz"])``
        """
        col = _field_to_column(self.field)
        if col is None:
            return ("1=1", [])

        params: list[Any] = []
        clause = ""

        op = self.operator.upper()
        if op in ("IS", "EQUALS", "=", "=="):
            clause = f"{col} = ?"
            params.append(self.value)
        elif op in ("ISNOT", "NOTEQUALS", "!=", "<>"):
            clause = f"{col} != ?"
            params.append(self.value)
        elif op in ("CONTAINS", "LIKE"):
            clause = f"{col} LIKE ?"
            params.append(f"%{self.value}%")
        elif op in ("STARTSWITH",):
            clause = f"{col} LIKE ?"
            params.append(f"{self.value}%")
        elif op in ("ENDSWITH",):
            clause = f"{col} LIKE ?"
            params.append(f"%{self.value}")
        elif op in ("GREATER", "GT", ">"):
            clause = f"{col} > ?"
            params.append(self.value)
        elif op in ("LESS", "LT", "<"):
            clause = f"{col} < ?"
            params.append(self.value)
        elif op in ("GTE", ">="):
            clause = f"{col} >= ?"
            params.append(self.value)
        elif op in ("LTE", "<="):
            clause = f"{col} <= ?"
            params.append(self.value)
        elif op in ("BETWEEN",):
            vals = self.value if isinstance(self.value, list) else [self.value]
            clause = f"{col} BETWEEN ? AND ?"
            params.extend(vals[:2])
        elif op in ("IN",):
            vals = self.value if isinstance(self.value, list) else [self.value]
            placeholders = ",".join("?" * len(vals))
            clause = f"{col} IN ({placeholders})"
            params.extend(vals)
        elif op in ("NOTIN",):
            vals = self.value if isinstance(self.value, list) else [self.value]
            placeholders = ",".join("?" * len(vals))
            clause = f"{col} NOT IN ({placeholders})"
            params.extend(vals)
        else:
            # Unknown operator — match all
            clause = "1=1"

        if self.negate:
            clause = f"NOT ({clause})"

        return (clause, params)

    # ---- internal ----

    def _get_track_field(self, track: TrackInfo) -> Any:
        """Get the raw field value from a track."""
        FIELD_MAP: dict[str, str] = {
            "title": "title",
            "artist": "artist",
            "album": "album",
            "albumartist": "album_artist",
            "genre": "genre",
            "year": "year",
            "tracknumber": "track_number",
            "discnumber": "disc_number",
            "format": "format",
            "bitrate": "bitrate",
            "samplerate": "sample_rate",
            "channels": "channels",
            "comment": "comment",
            "compilation": "compilation",
            "releasetype": "release_type",
        }
        attr = FIELD_MAP.get(self.field.lower())
        if attr and hasattr(track, attr):
            return getattr(track, attr)
        return None

    def _apply_operator(self, track_val: Any) -> bool:
        """Apply this rule's operator to a track field value."""
        rule_val = self.value
        op = self.operator.upper()

        if track_val is None:
            return op in ("IS", "=", "==")

        # Normalise to strings for string comparisons
        t = str(track_val).lower().strip()
        r = str(rule_val).lower().strip()

        if op in ("IS", "EQUALS", "=", "=="):
            return t == r
        if op in ("ISNOT", "NOTEQUALS", "!=", "<>"):
            return t != r
        if op in ("CONTAINS", "LIKE"):
            return r in t
        if op in ("STARTSWITH",):
            return t.startswith(r)
        if op in ("ENDSWITH",):
            return t.endswith(r)
        if op in ("GREATER", "GT", ">"):
            try:
                return float(t) > float(r)
            except (ValueError, TypeError):
                return False
        if op in ("LESS", "LT", "<"):
            try:
                return float(t) < float(r)
            except (ValueError, TypeError):
                return False
        if op in ("GTE", ">="):
            try:
                return float(t) >= float(r)
            except (ValueError, TypeError):
                return False
        if op in ("LTE", "<="):
            try:
                return float(t) <= float(r)
            except (ValueError, TypeError):
                return False
        if op in ("BETWEEN",):
            vals = rule_val if isinstance(rule_val, list) else [rule_val]
            if len(vals) >= 2:
                try:
                    tv = float(t)
                    return float(str(vals[0])) <= tv <= float(str(vals[1]))
                except (ValueError, TypeError):
                    return False
        if op in ("IN",):
            vals = rule_val if isinstance(rule_val, list) else [rule_val]
            return t in [str(v).lower() for v in vals]
        if op in ("NOTIN",):
            vals = rule_val if isinstance(rule_val, list) else [rule_val]
            return t not in [str(v).lower() for v in vals]

        return True


# ---- Column mapping ----

def _field_to_column(field: str) -> str | None:
    """Map a rule field name to a tracks table column name."""
    MAP: dict[str, str] = {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "albumartist": "album_artist",
        "genre": "genre",
        "year": "year",
        "tracknumber": "track_number",
        "discnumber": "disc_number",
        "format": "format",
        "bitrate": "bitrate",
        "samplerate": "sample_rate",
        "channels": "channels",
        "comment": "comment",
        "compilation": "compilation",
        "releasetype": "release_type",
    }
    return MAP.get(field.lower())


# ---------------------------------------------------------------------------
# VirtualLibrary repository
# ---------------------------------------------------------------------------

class VirtualLibraryRepository:
    """
    Async CRUD repository for virtual libraries.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    async def ensure_schema(self) -> None:
        """Create the virtual_libraries table if it doesn't exist."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS virtual_libraries (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT UNIQUE NOT NULL,
                    description TEXT DEFAULT '',
                    match_all   INTEGER DEFAULT 1,
                    created_at  INTEGER DEFAULT (strftime('%s', 'now')),
                    updated_at  INTEGER DEFAULT (strftime('%s', 'now'))
                );

                CREATE TABLE IF NOT EXISTS library_rules (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    library_id      INTEGER NOT NULL,
                    field           TEXT NOT NULL,
                    operator        TEXT NOT NULL,
                    value           TEXT NOT NULL,
                    negate          INTEGER DEFAULT 0,
                    sort_order      INTEGER DEFAULT 0,
                    FOREIGN KEY (library_id) REFERENCES virtual_libraries(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_library_rules_lib ON library_rules(library_id);
            """)
            await db.commit()

    async def save(self, lib: VirtualLibrary) -> int:
        """Save a virtual library (insert or update). Returns its ID."""
        await self.ensure_schema()
        async with aiosqlite.connect(str(self.db_path)) as db:
            now = int(db.execute("SELECT strftime('%s', 'now')").fetchone()[0])  # sync for now
            if lib.id is None:
                cur = await db.execute(
                    "INSERT INTO virtual_libraries (name, description, match_all, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (lib.name, lib.description, int(lib.match_all), now, now),
                )
                lib_id = cur.lastrowid or 0
            else:
                lib_id = lib.id
                await db.execute(
                    "UPDATE virtual_libraries SET name = ?, description = ?, match_all = ?, updated_at = ? WHERE id = ?",
                    (lib.name, lib.description, int(lib.match_all), now, lib_id),
                )

            # Replace rules
            await db.execute(
                "DELETE FROM library_rules WHERE library_id = ?", (lib_id,)
            )
            for i, rule in enumerate(lib.rules):
                await db.execute(
                    "INSERT INTO library_rules (library_id, field, operator, value, negate, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                    (lib_id, rule.field, rule.operator, str(rule.value), int(rule.negate), i),
                )

            await db.commit()
            lib.id = lib_id
            lib.updated_at = now
            return lib_id

    async def delete(self, lib_id: int) -> bool:
        """Delete a virtual library by ID. Returns True if it existed."""
        await self.ensure_schema()
        async with aiosqlite.connect(str(self.db_path)) as db:
            cur = await db.execute(
                "DELETE FROM virtual_libraries WHERE id = ?", (lib_id,)
            )
            await db.commit()
            return cur.rowcount > 0

    async def by_id(self, lib_id: int) -> VirtualLibrary | None:
        """Load a virtual library by ID."""
        await self.ensure_schema()
        async with aiosqlite.connect(str(self.db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM virtual_libraries WHERE id = ?", (lib_id,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None

        rules = await self._load_rules(row["id"])
        return VirtualLibrary(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            match_all=bool(row["match_all"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            rules=rules,
        )

    async def all(self) -> list[VirtualLibrary]:
        """Return all saved virtual libraries."""
        await self.ensure_schema()
        libs: list[VirtualLibrary] = []
        async with aiosqlite.connect(str(self.db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id FROM virtual_libraries ORDER BY name"
            ) as cur:
                rows = await cur.fetchall()

        for row in rows:
            lib = await self.by_id(row["id"])
            if lib:
                libs.append(lib)
        return libs

    async def query_tracks(
        self,
        lib: VirtualLibrary,
        db_path: Path,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[TrackInfo]:
        """
        Execute the virtual library as a track query against the DB.

        Parameters
        ----------
        lib
            The virtual library to query.
        db_path
            Path to the tracks database.
        limit
            Maximum number of tracks to return.
        offset
            SQL OFFSET for pagination.

        Returns
        -------
        list[TrackInfo]
        """
        repo = TrackRepository(db_path)
        clause, params = lib.build_where_clause()
        sql = f"SELECT * FROM tracks WHERE {clause} ORDER BY artist, album, disc_number, track_number LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows: list[dict[str, Any]] = []
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(sql, params) as cur:
                    rows = await cur.fetchall()
        except Exception:  # noqa: BLE001
            logger.exception("Virtual library query failed")
            return []

        return [TrackRepository._row_to_track(dict(r)) for r in rows]

    async def _load_rules(self, lib_id: int) -> list[LibraryRule]:
        """Load rules for a virtual library."""
        rules: list[LibraryRule] = []
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM library_rules WHERE library_id = ? ORDER BY sort_order",
                    (lib_id,),
                ) as cur:
                    async for row in cur:
                        rules.append(LibraryRule(
                            field=row["field"],
                            operator=row["operator"],
                            value=row["value"],
                            negate=bool(row["negate"]),
                        ))
        except Exception:  # noqa: BLE001
            logger.exception("Error loading library rules")
        return rules
