"""
Preferences system for Pyrion Music Server.

Ported from Slim::Utils::Prefs. Provides SQLite-backed preference storage
with categories, defaults, validation, and change callbacks.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Awaitable, ParamSpec, TypeVar
from collections import defaultdict
from dataclasses import dataclass, field
from functools import wraps
import re

import aiosqlite

logger = logging.getLogger("lyrion.prefs")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

P = ParamSpec("P")
R = TypeVar("R")

#: Preference type names (matching LMS)
PREF_TYPE_STRING = "string"
PREF_TYPE_INT = "int"
PREF_TYPE_FLOAT = "float"
PREF_TYPE_BOOL = "bool"
PREF_TYPE_LIST = "list"
PREF_TYPE_HASH = "hash"
PREF_TYPE_CHOICE = "choice"

#: Preference categories
PREF_CAT_SERVER = "server"
PREF_CAT_PLUGIN = "plugin"
PREF_CAT_PLAYER = "player"
PREF_CAT_UI = "ui"
PREF_CAT_I18N = "i18n"
PREF_CAT_LIBRARY = "library"


@dataclass
class PrefMeta:
    """Metadata for a registered preference."""
    name: str
    default: Any = None
    type: str = PREF_TYPE_STRING
    category: str = ""
    validate_expr: str | None = None
    validate_func: str | None = None
    choices: list[Any] | None = None
    changed_callback: Callable[[Any, Any], None] | None = None


@dataclass
class ChangeCallback:
    """A registered preference change callback."""
    pref_name: str
    callback: Callable[[Any, Any], None]
    one_shot: bool = False


# ---------------------------------------------------------------------------
# PreferenceStore
# ---------------------------------------------------------------------------

class PreferenceStore:
    """
    SQLite-backed preference store.

    Supports:
    - Types: string, int, float, bool, list, hash
    - Validation via regex or callable
    - Category organization
    - Change callbacks
    - Import/export
    - Persisted to SQLite
    """

    __slots__ = (
        "_db_path",
        "_conn",
        "_cache",
        "_lock",
        "_callbacks",
        "_categories",
        "_init_done",
        "_prefs_dir",
    )

    _instance: PreferenceStore | None = None

    def __init__(
        self,
        db_path: Path | str | None = None,
        prefs_dir: Path | str | None = None,
    ) -> None:
        self._db_path: Path | None = Path(db_path) if db_path else None
        self._prefs_dir: Path = Path(prefs_dir) if prefs_dir else Path.home() / ".lyrion" / "Prefs"
        self._conn: aiosqlite.Connection | None = None
        self._cache: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._callbacks: list[ChangeCallback] = []
        self._categories: dict[str, dict[str, PrefMeta]] = defaultdict(dict)
        self._init_done = False

    @classmethod
    def instance(cls) -> PreferenceStore:
        """Return the singleton PreferenceStore instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for testing)."""
        inst = cls._instance
        if inst is not None:
            if inst._conn is not None:
                inst._conn.close()
        cls._instance = None

    # ---- lifecycle ----

    async def init(self) -> None:
        """Initialize the SQLite database."""
        if self._init_done:
            return

        if self._db_path is None:
            self._prefs_dir.mkdir(parents=True, exist_ok=True)
            self._db_path = self._prefs_dir / "prefs.db"

        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row

        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS prefmeta (
                name         TEXT PRIMARY KEY,
                default_value TEXT,
                type         TEXT NOT NULL DEFAULT 'string',
                category     TEXT NOT NULL DEFAULT '',
                validate_expr TEXT,
                validate_func TEXT,
                choices      TEXT,
                modified     INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS prefhash (
                name         TEXT PRIMARY KEY,
                value        TEXT,
                modified     INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS meta (
                key          TEXT PRIMARY KEY,
                value        TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_pm_category ON prefmeta(category);
            CREATE INDEX IF NOT EXISTS idx_ph_modified ON prefhash(modified);
        """)
        await self._conn.commit()

        # Load cached values
        await self._load_cache()
        self._init_done = True
        logger.info("PreferenceStore initialized at %s", self._db_path)

    async def _load_cache(self) -> None:
        """Load all persisted preferences into memory cache."""
        if self._conn is None:
            return
        async with self._conn.execute(
            "SELECT name, value FROM prefhash"
        ) as cur:
            rows = await cur.fetchall()
        with self._lock:
            for row in rows:
                self._cache[row["name"]] = self._deserialize(
                    row["name"], row["value"]
                )

    async def _save(self, name: str, value: Any) -> None:
        """Persist a preference to SQLite."""
        if self._conn is None:
            raise RuntimeError("PreferenceStore not initialized")
        serialized = self._serialize(value)
        await self._conn.execute(
            "INSERT OR REPLACE INTO prefhash (name, value, modified) VALUES (?, ?, 1)",
            (name, serialized),
        )
        await self._conn.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- serialization ----

    def _serialize(self, value: Any) -> str:
        """Serialize a value to string for SQLite storage."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (list, dict)):
            import json
            return json.dumps(value, separators=(",", ":"))
        return str(value)

    def _deserialize(self, name: str, value: str | None) -> Any:
        """Deserialize a value from SQLite storage."""
        if value is None or value == "":
            return None

        meta = self._categories.get("", {}).get(name)
        if meta is None:
            meta = self._categories.get(PREF_CAT_PLUGIN, {}).get(name)

        if meta is not None:
            return self._coerce(meta.type, value)

        # Try numeric
        try:
            return int(value)
        except (ValueError, TypeError):
            pass
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
        # Boolean
        if value in ("1", "yes", "true", "on"):
            return True
        if value in ("0", "no", "false", "off"):
            return False
        # List/hash
        if value.startswith(("[", "{")):
            import json
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
        return value

    @staticmethod
    def _coerce(type_name: str, value: str | Any) -> Any:
        """Coerce a raw value to the correct Python type."""
        if isinstance(value, str):
            if type_name == PREF_TYPE_INT:
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return 0
            if type_name == PREF_TYPE_FLOAT:
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return 0.0
            if type_name == PREF_TYPE_BOOL:
                return value in ("1", "yes", "true", "on")
            if type_name == PREF_TYPE_LIST:
                if value.startswith("["):
                    import json
                    try:
                        return json.loads(value)
                    except json.JSONDecodeError:
                        return []
                return [v.strip() for v in value.split(",") if v.strip()]
            if type_name == PREF_TYPE_HASH:
                import json
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return {}
        return value

    # ---- preference registration ----

    async def add(
        self,
        name: str,
        default: Any = None,
        *,
        type: str = PREF_TYPE_STRING,
        category: str = "",
        validate_expr: str | None = None,
        validate_func: str | None = None,
        choices: list[Any] | None = None,
    ) -> None:
        """
        Register a preference with metadata (idempotent).

        Must be called before accessing the preference to ensure the
        default is set in the DB.
        """
        if self._conn is None:
            return

        meta = PrefMeta(
            name=name,
            default=default,
            type=type,
            category=category,
            validate_expr=validate_expr,
            validate_func=validate_func,
            choices=choices,
        )
        self._categories[category][name] = meta

        # Insert into DB if not exists
        default_str = self._serialize(default)
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO prefmeta
            (name, default_value, type, category, validate_expr, validate_func, choices)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, default_str, type, category, validate_expr, validate_func, None),
        )

        # If not in prefhash, set default
        if name not in self._cache:
            self._cache[name] = default
            if default is not None:
                await self._save(name, default)
        else:
            # Ensure it's coerced to the right type
            self._cache[name] = self._coerce(type, self._cache[name])

        await self._conn.commit()

    # ---- preference access ----

    def get(self, name: str, default: Any = None) -> Any:
        """
        Get a preference value.

        Returns the cached value, default, or registered default.
        """
        if name in self._cache:
            return self._cache[name]

        # Look up registered default
        for cats in self._categories.values():
            if name in cats:
                return cats[name].default

        return default

    async def set(self, name: str, value: Any) -> None:
        """Set a preference value and persist it."""
        # Validate
        for cats in self._categories.values():
            if name in cats:
                meta = cats[name]
                if meta.choices and value not in meta.choices:
                    raise ValueError(
                        f"Preference '{name}' must be one of {meta.choices}, got {value!r}"
                    )
                if meta.validate_expr:
                    if not re.match(meta.validate_expr, str(value)):
                        raise ValueError(
                            f"Preference '{name}' value {value!r} does not match pattern {meta.validate_expr!r}"
                        )

        old_value = self._cache.get(name)
        self._cache[name] = value
        await self._save(name, value)

        # Fire callbacks
        await self._fire_callbacks(name, old_value, value)

    def __contains__(self, name: str) -> bool:
        return name in self._cache

    def __getitem__(self, name: str) -> Any:
        return self.get(name)

    async def __setitem__(self, name: str, value: Any) -> None:
        await self.set(name, value)

    # ---- callbacks ----

    def add_changed_callback(
        self,
        pref_name: str,
        callback: Callable[[Any, Any], None],
        *,
        one_shot: bool = False,
    ) -> None:
        """Register a callback for when a preference changes."""
        self._callbacks.append(
            ChangeCallback(pref_name, callback, one_shot)
        )

    async def _fire_callbacks(
        self,
        name: str,
        old_value: Any,
        new_value: Any,
    ) -> None:
        """Fire all callbacks registered for a preference change."""
        if old_value == new_value:
            return

        callbacks_to_remove: list[ChangeCallback] = []
        for cb in self._callbacks:
            if cb.pref_name == name or cb.pref_name == "*":
                try:
                    result = cb.callback(old_value, new_value)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.warning(
                        "Preference callback for '%s' raised: %s", name, e
                    )
                if cb.one_shot:
                    callbacks_to_remove.append(cb)

        for cb in callbacks_to_remove:
            self._callbacks.remove(cb)

    # ---- bulk operations ----

    async def all_in_category(self, category: str) -> dict[str, Any]:
        """Return all preferences in a category."""
        result = {}
        for name in self._cache:
            for cats in self._categories.values():
                if name in cats and cats[name].category == category:
                    result[name] = self._cache[name]
                    break
        return result

    async def export_all(self) -> dict[str, Any]:
        """Export all preferences as a dict."""
        return dict(self._cache)

    async def import_all(self, data: dict[str, Any]) -> None:
        """Import preferences from a dict."""
        for name, value in data.items():
            await self.set(name, value)

    # ---- utility ----

    def keys(self) -> list[str]:
        """Return all known preference keys."""
        return list(self._cache.keys())


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_preference_store: PreferenceStore = PreferenceStore.instance()


def get_prefs() -> PreferenceStore:
    """Return the global PreferenceStore instance."""
    return _preference_store
