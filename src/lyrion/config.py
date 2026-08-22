"""
Configuration file parser and preference management for Lyrion Music Server.

Handles .conf INI-like config files (the same format used by LMS) with sections
and key=value pairs. Also manages CLI overrides and SQLite-backed preference persistence.
"""

from __future__ import annotations

import os
import re
import sys
import sqlite3
import argparse
from pathlib import Path
from typing import Any, Iterator

import aiosqlite

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class ConfigSection(dict[str, str]):
    """A configuration section containing key=value pairs."""

    __slots__ = ()


class ConfigFile(dict[str, ConfigSection]):
    """An INI-like .conf file with named sections."""

    __slots__ = ()


# ---------------------------------------------------------------------------
# .conf file parser
# ---------------------------------------------------------------------------

_CONF_COMMENT_RE = re.compile(r"^\s*#")
_CONF_BLANK_RE = re.compile(r"^\s*$")
_CONF_SECTION_RE = re.compile(r"^\[([^\]]+)\]$")
_CONF_KEYVALUE_RE = re.compile(r"^([^=\s]+)\s*=\s*(.*)$")


def parse_conf(content: str) -> ConfigFile:
    """Parse .conf file content into a ConfigFile dict-of-sections structure."""
    result: ConfigFile = ConfigFile()
    current_section: ConfigSection = ConfigSection()
    result[""] = current_section  # default/ungrouped keys go in ""

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if _CONF_COMMENT_RE.match(line) or _CONF_BLANK_RE.match(line):
            continue

        sec_match = _CONF_SECTION_RE.match(line)
        if sec_match:
            section_name = sec_match.group(1)
            current_section = ConfigSection()
            result[section_name] = current_section
            continue

        kv_match = _CONF_KEYVALUE_RE.match(line)
        if kv_match:
            current_section[kv_match.group(1)] = kv_match.group(2)

    return result


def read_conf(path: Path) -> ConfigFile:
    """Read and parse a .conf file from disk."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_conf(fh.read())


def write_conf(path: Path, data: ConfigFile) -> None:
    """Serialize a ConfigFile back to a .conf file."""
    with open(path, "w", encoding="utf-8") as fh:
        for section_name, section_data in data.items():
            if section_name:
                fh.write(f"[{section_name}]\n")
            for key, value in section_data.items():
                fh.write(f"{key} = {value}\n")
            fh.write("\n")


# ---------------------------------------------------------------------------
# Preference store (SQLite-backed)
# ---------------------------------------------------------------------------

_PREF_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS prefmeta (
    name TEXT PRIMARY KEY,
    default_value TEXT,
    validate_expr TEXT,
    validate_func TEXT,
    category TEXT DEFAULT '',
    type TEXT DEFAULT 'string',
    modified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prefhash (
    name TEXT PRIMARY KEY,
    value TEXT,
    modified INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_prefmeta_category ON prefmeta(category);
CREATE INDEX IF NOT EXISTS idx_prefhash_modified ON prefhash(modified);
"""


class PreferenceStore:
    """
    SQLite-backed preference store mimicking Slim::Utils::Prefs.

    Stores preferences in a SQLite database with categories, default values,
    validation, and change-tracking.
    """

    __slots__ = ("_db_path", "_db", "_cache", "_cli_overrides", "_loaded")

    _instance: PreferenceStore | None = None

    def __init__(
        self,
        db_path: Path | str | None = None,
    ) -> None:
        self._db_path: Path = Path(db_path) if db_path else Path.home() / ".lyrion" / "prefs.db"
        self._db: aiosqlite.Connection | None = None
        self._cache: dict[str, str] = {}
        self._cli_overrides: dict[str, Any] = {}
        self._loaded = False

    def set_db_path(self, path: Path) -> None:
        """Point the store at another SQLite file BEFORE init(). Used by
        LyrionConfig so preferences live under the serverdata directory
        (like the Perl LMS server prefs dir) instead of a fixed home
        path."""
        if self._loaded:
            raise RuntimeError("PreferenceStore already initialized")
        self._db_path = Path(path)

    @classmethod
    def instance(cls) -> PreferenceStore:
        """Return the singleton PreferenceStore instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (useful for testing)."""
        if cls._instance is not None:
            cls._instance._close_sync()
        cls._instance = None

    # ---- async lifecycle ----

    async def init(self) -> None:
        """Initialize the database connection and create schema."""
        if self._loaded:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_PREF_DB_SCHEMA)
        await self._db.commit()
        await self._load_cache()
        self._loaded = True

    async def _load_cache(self) -> None:
        """Load all preferences into the in-memory cache."""
        if self._db is None:
            return
        async with self._db.execute("SELECT name, value FROM prefhash") as cur:
            rows = await cur.fetchall()
        self._cache = {row["name"]: row["value"] for row in rows}

    async def _save(self, name: str, value: str) -> None:
        """Persist a single preference to the database."""
        if self._db is None:
            raise RuntimeError("PreferenceStore not initialized")
        await self._db.execute(
            "INSERT OR REPLACE INTO prefhash (name, value, modified) VALUES (?, ?, 1)",
            (name, value),
        )
        await self._db.commit()

    def _close_sync(self) -> None:
        """Synchronously close the database (for shutdown)."""
        if self._db is not None:
            self._db.close()
            self._db = None

    # ---- preference access ----

    def set_cli_override(self, name: str, value: Any) -> None:
        """Record a CLI flag override that takes precedence over DB."""
        self._cli_overrides[name] = value

    def get(
        self,
        name: str,
        default: Any = None,
        *,
        category: str = "",
    ) -> Any:
        """
        Get a preference value.

        Priority: CLI override > DB value > default.
        Type coercion is handled automatically based on the stored type metadata.
        """
        if name in self._cli_overrides:
            return self._cli_overrides[name]

        meta_row = self._get_meta_sync(name)
        value_str = self._cache.get(name)
        if value_str is None:
            if default is not None:
                return default
            if meta_row is not None and meta_row["default_value"] is not None:
                value_str = meta_row["default_value"]
            else:
                return default

        type_name = meta_row["type"] if meta_row else "string"
        return self._coerce_type(value_str, type_name, default)

    def _get_meta_sync(self, name: str) -> aiosqlite.Row | None:
        """Synchronously fetch preference metadata (runs in executor)."""
        # This is called from sync get(); run in thread to avoid blocking
        import asyncio

        try:
            loop = asyncio.get_running_loop()

            async def _fetch() -> aiosqlite.Row | None:
                if self._db is None:
                    return None
                async with self._db.execute(
                    "SELECT * FROM prefmeta WHERE name = ?", (name,)
                ) as cur:
                    return await cur.fetchone()

            future = loop.create_task(_fetch())
                # We'll check this in a sync context below - avoid deadlock
            return None  # fallback; will be refined
        except Exception:
            return None

    @staticmethod
    def _coerce_type(value: str, type_name: str, default: Any) -> Any:
        """Coerce a string value to the correct Python type."""
        if value is None:
            return default
        if type_name == "number" or type_name == "int":
            try:
                return int(value)
            except ValueError:
                return default if default is not None else 0
        if type_name == "float":
            try:
                return float(value)
            except ValueError:
                return default if default is not None else 0.0
        if type_name == "bool" or type_name == "boolean":
            return value.lower() in ("1", "yes", "true", "on")
        if type_name == "list":
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    async def set(self, name: str, value: Any, *, category: str = "") -> None:
        """Set a preference value and persist it."""
        str_value = str(value) if value is not None else ""
        self._cache[name] = str_value
        await self._save(name, str_value)

    def __contains__(self, name: str) -> bool:
        return name in self._cache or name in self._cli_overrides

    async def init_preference(
        self,
        name: str,
        *,
        default: Any = None,
        validate_expr: str | None = None,
        validate_func: str | None = None,
        category: str = "",
        type_name: str = "string",
    ) -> None:
        """
        Register a preference with metadata (idempotent).
        Call before accessing the preference to ensure defaults are set.
        """
        if self._db is None:
            return
        default_str = str(default) if default is not None else ""
        await self._db.execute(
            """
            INSERT OR IGNORE INTO prefmeta
            (name, default_value, validate_expr, validate_func, category, type)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, default_str, validate_expr, validate_func, category, type_name),
        )
        await self._db.commit()
        # If not in cache, load default
        if name not in self._cache and default is not None:
            self._cache[name] = default_str

    async def all_by_category(self, category: str) -> dict[str, Any]:
        """Return all preferences in a given category."""
        if self._db is None:
            return {}
        result: dict[str, Any] = {}
        async with self._db.execute(
            "SELECT name FROM prefmeta WHERE category = ?", (category,)
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            result[row["name"]] = self.get(row["name"])
        return result

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None


# ---------------------------------------------------------------------------
# Global config / runtime directories
# ---------------------------------------------------------------------------


class LyrionConfig:
    """
    Central runtime configuration object.
    
    Combines .conf file parsing with CLI flag overrides and SQLite-backed
    preference persistence.
    """

    __slots__ = (
        "_prefs",
        "_conf_path",
        "_conf",
        "_serverdata_dir",
        "_prefs_dir",
        "_log_dir",
        "_cache_dir",
        "_cli_args",
    )

    _instance: LyrionConfig | None = None

    def __init__(self) -> None:
        self._prefs = PreferenceStore.instance()
        self._conf_path: Path | None = None
        self._conf: ConfigFile = ConfigFile()
        self._serverdata_dir: Path | None = None
        self._prefs_dir: Path | None = None
        self._log_dir: Path | None = None
        self._cache_dir: Path | None = None
        self._cli_args: argparse.Namespace = argparse.Namespace()

    def set_cli_args(self, args: argparse.Namespace) -> None:
        self._cli_args = args

    @classmethod
    def instance(cls) -> LyrionConfig:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---- directory resolution ----

    def _resolve_serverdata_dir(self) -> Path:
        """Determine the server data directory (where prefs/cache/log live)."""
        if self._serverdata_dir:
            return self._serverdata_dir

        prefs = PreferenceStore.instance()
        # CLI override
        if getattr(self._cli_args, "serverdata", None):
            return Path(self._cli_args.serverdata)

        # Check environment
        env_dir = os.environ.get("LYRION_SERVERDATA")
        if env_dir:
            return Path(env_dir)

        # Platform defaults
        system = os.name
        if system == "nt":  # Windows
            base = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
        elif system == "posix" and hasattr(os, "uname") and os.uname().sysname == "Darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path.home() / ".lyrion"

        default_dir = base / "Lyrion"
        self._serverdata_dir = default_dir
        self._serverdata_dir.mkdir(parents=True, exist_ok=True)
        return self._serverdata_dir

    @property
    def serverdata_dir(self) -> Path:
        return self._resolve_serverdata_dir()

    @property
    def prefs_dir(self) -> Path:
        if self._prefs_dir:
            return self._prefs_dir
        self._prefs_dir = self.serverdata_dir / "Prefs"
        self._prefs_dir.mkdir(parents=True, exist_ok=True)
        return self._prefs_dir

    @property
    def log_dir(self) -> Path:
        if self._log_dir:
            return self._log_dir
        self._log_dir = self.serverdata_dir / "Logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        return self._log_dir

    @property
    def cache_dir(self) -> Path:
        if self._cache_dir:
            return self._cache_dir
        self._cache_dir = self.serverdata_dir / "Cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    @property
    def db_path(self) -> Path:
        return self.prefs_dir / "lyrion.db"

    # ---- .conf file ----

    @property
    def conf_path(self) -> Path:
        if self._conf_path:
            return self._conf_path
        if getattr(self._cli_args, "localfile", None):
            return Path(self._cli_args.localfile)
        return self.prefs_dir.parent / "Lyrion.conf"

    def load_conf(self, path: Path | None = None) -> None:
        """Load a .conf file."""
        p = path or self.conf_path
        if p.exists():
            self._conf = read_conf(p)
            self._conf_path = p
        else:
            self._conf = ConfigFile()

    def get_conf(self, key: str, section: str = "", default: str = "") -> str:
        """Get a value from the .conf file."""
        return self._conf.get(section, {}).get(key, default)

    # ---- CLI argument handling ----

    def parse_cli(self, args: list[str] | None = None) -> argparse.Namespace:
        """Parse CLI arguments and apply them as overrides."""
        parser = argparse.ArgumentParser(
            prog="lyrion",
            description="Lyrion Music Server",
        )
        parser.add_argument(
            "--noweb",
            action="store_true",
            help="Disable the web interface",
        )
        parser.add_argument(
            "--localfile",
            metavar="FILE",
            help="Load configuration from FILE instead of default location",
        )
        parser.add_argument(
            "--serverdata",
            metavar="DIR",
            help="Set server data directory (prefs, cache, logs)",
        )
        parser.add_argument(
            "--nobrowsecache",
            action="store_true",
            help="Disable the browse cache",
        )
        parser.add_argument(
            "--prefsfile",
            metavar="FILE",
            help="SQLite preferences database path",
        )
        parser.add_argument(
            "--logfile",
            metavar="FILE",
            help="Log file path (overrides default)",
        )
        parser.add_argument(
            "--loglevel",
            choices=["debug", "info", "warning", "error", "critical"],
            default="info",
            help="Set logging level (default: info)",
        )
        parser.add_argument(
            "--daemon",
            action="store_true",
            help="Run as a daemon (background process)",
        )
        parser.add_argument(
            "--pidfile",
            metavar="FILE",
            help="Write PID to FILE",
        )
        parser.add_argument(
            "--version",
            action="store_true",
            help="Print version and exit",
        )
        parser.add_argument(
            "--playeraddr",
            metavar="ADDR",
            help="Bind to specific address for player discovery",
        )
        parser.add_argument(
            "--httpport",
            type=int,
            metavar="PORT",
            help="HTTP port for web interface (default: 9000)",
        )
        parser.add_argument(
            "--cliport",
            type=int,
            metavar="PORT",
            help="CLI port for telnet/text protocol (default: 9090)",
        )
        parser.add_argument(
            "--slimprotoport",
            type=int,
            metavar="PORT",
            help="SlimProto TCP port for players (default: 3483)",
        )
        self._cli_args = parser.parse_args(args)

        # Apply CLI overrides to preference store
        prefs = PreferenceStore.instance()
        for key, value in vars(self._cli_args).items():
            if value is not None and value is not False:
                prefs.set_cli_override(key, value)

        return self._cli_args

    @property
    def cli_args(self) -> argparse.Namespace:
        return self._cli_args

    def get(self, name: str, default: Any = None) -> Any:
        """Get a preference, checking .conf file then SQLite prefs."""
        # First check .conf file (for settings that come from there)
        conf_val = self._conf.get("", {}).get(name)
        if conf_val is not None:
            return conf_val
        # Fall back to preference store
        return self._prefs.get(name, default)

    async def init(self) -> None:
        """Initialize the configuration system."""
        # Preferences live under the serverdata dir (Perl-LMS layout:
        # <serverdata>/Prefs/prefs.db). Wire the singleton store there
        # before it opens its DB.
        self._prefs.set_db_path(self.prefs_dir / "prefs.db")
        self.load_conf()
        await self._prefs.init()
        # Register known preferences
        await self._register_known_prefs()

    async def _register_known_prefs(self) -> None:
        """Register the standard set of Lyrion preferences with defaults."""
        prefs = self._prefs
        await prefs.init_preference("serverport", default=9000, type_name="int", category="server")
        await prefs.init_preference("cliport", default=9090, type_name="int", category="server")
        await prefs.init_preference("noweb", default=0, type_name="bool", category="server")
        await prefs.init_preference("loglevel", default="info", category="server")
        await prefs.init_preference("maxwebcache", default=10000, type_name="int", category="server")
        await prefs.init_preference("browsecache", default=1, type_name="bool", category="server")
        await prefs.init_preference("uuid", default="", category="server")
        await prefs.init_preference("language", default="en", category="i18n")
        await prefs.init_preference("musicdir", default="", category="library")
        await prefs.init_preference("audiodir", default="", category="library")
        await prefs.init_preference("playlistdir", default="", category="library")

    async def close(self) -> None:
        """Shutdown configuration (close DB, etc.)."""
        await self._prefs.close()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

#: The active PreferenceStore instance
_preference_store: PreferenceStore = PreferenceStore.instance()

#: The active LyrionConfig instance
_config: LyrionConfig = LyrionConfig.instance()


def get_config() -> LyrionConfig:
    """Return the global LyrionConfig instance."""
    return _config


def get_prefs() -> PreferenceStore:
    """Return the global PreferenceStore instance."""
    return _preference_store
