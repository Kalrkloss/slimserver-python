"""
Configuration file parser and preference management for Pyrion Music Server.

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

def _legacy_or_default_prefs_db() -> Path:
    """Where the singleton :class:`PreferenceStore` opens its DB before init.

    Order: the legacy per-user file (``~/.lyrion/prefs.db`` — the location every
    version before the platform layer used) when it exists, else the file inside
    the OS-specific data root.  Never creates anything.
    """
    from lyrion.platform import paths as _paths

    legacy = Path.home() / ".lyrion" / "prefs.db"
    if legacy.is_file():
        return legacy
    return _paths.port_dir("prefs") / "prefs.db"


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

    __slots__ = ("_db_path", "_db", "_cache", "_meta_cache", "_cli_overrides", "_loaded")

    _instance: PreferenceStore | None = None

    def __init__(
        self,
        db_path: Path | str | None = None,
    ) -> None:
        # Until ``LyrionConfig.init()`` points the store at the resolved
        # serverdata directory, the legacy per-user location is used.  An
        # existing legacy file wins, so an upgrade keeps its preferences
        # (Perl's ``migratePrefsFolder``, ``Slim/Utils/OS/Unix.pm:115-127``,
        # does the same for prefs folders).
        self._db_path: Path = (Path(db_path) if db_path
                               else _legacy_or_default_prefs_db())
        self._db: aiosqlite.Connection | None = None
        self._cache: dict[str, str] = {}
        # prefmeta rows (name -> row) for synchronous type coercion.
        self._meta_cache: dict[str, dict] = {}
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
        # Cache prefmeta (types/defaults) too, so the sync get() can coerce
        # int/bool prefs without an async fetch.
        async with self._db.execute(
                "SELECT name, default_value, type FROM prefmeta") as cur2:
            meta_rows = await cur2.fetchall()
        self._meta_cache = {
            r["name"]: {"name": r["name"], "default_value": r["default_value"],
                        "type": r["type"]}
            for r in meta_rows
        }

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

    def _get_meta_sync(self, name: str) -> dict | None:
        """Return the cached prefmeta row for ``name`` (for type coercion).

        The meta rows are loaded into ``_meta_cache`` at init, so the sync
        ``get()`` never needs an async fetch (the old implementation created
        a task it never awaited and always returned None → every pref came
        back as a string).
        """
        return self._meta_cache.get(name)

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
        # Keep the in-memory meta cache in sync so sync get() sees the type.
        self._meta_cache[name] = {
            "name": name, "default_value": default_str, "type": type_name,
        }

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
        "_serverdata_source",
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
        self._serverdata_source: str = ""
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
        """Determine the server data directory (where prefs/cache/log live).

        Delegates to :func:`lyrion.platform.paths.resolve_serverdata_dir`, which
        applies the Perl-shaped rules: ``--serverdata`` (Perl's ``--prefsdir``,
        ``Slim/Utils/Prefs.pm:89-92``) → ``$LYRION_SERVERDATA`` → an existing
        data directory of an earlier version (migration) → the OS default
        (Windows ``%ProgramData%\\Lyrion``, macOS
        ``~/Library/Application Support/Squeezebox`` — ``Slim/Utils/OS/Win32.pm:485-560``,
        ``Slim/Utils/OS/OSX.pm:175`` — Linux/Unix ``~/.lyrion/Lyrion``).
        """
        if self._serverdata_dir:
            return self._serverdata_dir

        from lyrion.platform import paths as _paths

        selected, source = _paths.resolve_serverdata_dir(
            cli_value=getattr(self._cli_args, "serverdata", None),
            environ=os.environ,
        )
        self._serverdata_source = source
        self._serverdata_dir = selected
        self._serverdata_dir.mkdir(parents=True, exist_ok=True)
        return self._serverdata_dir

    @property
    def serverdata_source(self) -> str:
        """Which rule chose :attr:`serverdata_dir` (``cli``/``env``/``migration``/``os-default``)."""
        self._resolve_serverdata_dir()
        return self._serverdata_source

    @property
    def serverdata_dir(self) -> Path:
        return self._resolve_serverdata_dir()

    def _explicit_serverdata(self) -> str | None:
        """The ``--serverdata`` / ``$LYRION_SERVERDATA`` root, when one was given."""
        cli = getattr(self._cli_args, "serverdata", None)
        if cli:
            return str(cli)
        return os.environ.get("LYRION_SERVERDATA") or None

    @property
    def prefs_dir(self) -> Path:
        if self._prefs_dir:
            return self._prefs_dir
        from lyrion.platform import paths as _paths

        self._prefs_dir = _paths.port_dir(
            "prefs", serverdata=self._explicit_serverdata())
        self._prefs_dir.mkdir(parents=True, exist_ok=True)
        return self._prefs_dir

    @property
    def log_dir(self) -> Path:
        if self._log_dir:
            return self._log_dir
        from lyrion.platform import paths as _paths

        self._log_dir = _paths.port_dir(
            "log", serverdata=self._explicit_serverdata())
        self._log_dir.mkdir(parents=True, exist_ok=True)
        return self._log_dir

    @property
    def cache_dir(self) -> Path:
        if self._cache_dir:
            return self._cache_dir
        from lyrion.platform import paths as _paths

        self._cache_dir = _paths.port_dir(
            "cache", serverdata=self._explicit_serverdata())
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
            description="Pyrion Music Server",
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
            "--public-http-port",
            type=int,
            metavar="PORT",
            help=("The ONE port every client is told about (default: 9000). "
                  "Set 0 together with --internal-http-port 9000 to roll the "
                  "single-port consolidation back (uvicorn serves it itself)."),
        )
        parser.add_argument(
            "--internal-http-port",
            type=int,
            metavar="PORT",
            help=("Internal port for the ASGI app behind the native frontend "
                  "(default: 9001, loopback only)."),
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
        # Single-port layout (Perl parity): every client is told about
        # `public_http_port` (the native, pipelining-capable frontend);
        # `internal_http_port` is where uvicorn listens — loopback only.
        # `web/api.py` (serverstatus.httpport), `networking/discovery.py`
        # (TLV) and the JSON presence beacon in `__main__.py` all announce the
        # public one (`config.http_ports()`).
        await prefs.init_preference(
            "public_http_port", default=DEFAULT_PUBLIC_HTTP_PORT,
            type_name="int", category="server")
        await prefs.init_preference(
            "internal_http_port", default=DEFAULT_INTERNAL_HTTP_PORT,
            type_name="int", category="server")
        await prefs.init_preference("cliport", default=9090, type_name="int", category="server")
        await prefs.init_preference("noweb", default=0, type_name="bool", category="server")
        await prefs.init_preference("loglevel", default="info", category="server")
        await prefs.init_preference("maxwebcache", default=10000, type_name="int", category="server")
        await prefs.init_preference("browsecache", default=1, type_name="bool", category="server")
        await prefs.init_preference("uuid", default="", category="server")
        await prefs.init_preference("password", default="", category="server")
        await prefs.init_preference("username", default="", category="server")
        await prefs.init_preference("authorize", default=0, type_name="bool", category="server")
        await prefs.init_preference("language", default="en", category="i18n")
        await prefs.init_preference("musicdir", default="", category="library")
        await prefs.init_preference("audiodir", default="", category="library")
        await prefs.init_preference("playlistdir", default="", category="library")
        # Media folders — Perl: ``Slim/Utils/Prefs.pm:102`` lists ``mediadirs``
        # among the filepath prefs, ``:163`` gives it the ``defaultMediaDirs``
        # default (``audiodir`` else the OS music folder, ``:687-712``) and
        # ``:207`` defaults ``ignoreInAudioScan`` to ``[]``; ``:383-403``
        # validates both as arrays of unique, existing folders.  Our store
        # serialises them comma separated (``web/settings.py``
        # ``_save_server_basic`` mirrors ``Server/Basic.pm:88-121``), hence
        # ``list``.  ``media/folders.py`` consumes them (``getMediaDirs``,
        # ``Slim/Utils/Misc.pm:727-756``).
        await prefs.init_preference(
            "mediadirs", default="", type_name="list", category="library")
        await prefs.init_preference(
            "ignoreInAudioScan", default="", type_name="list", category="library")
        # ``fileFilter`` skips entries matching this regex
        # (``Slim/Utils/Misc.pm:859-861``).
        await prefs.init_preference("ignoreDirRE", default="", category="library")

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


# ---------------------------------------------------------------------------
# HTTP port layout (Perl parity: ONE public port)
# ---------------------------------------------------------------------------

#: The ONE port every client is told about — Perl's ``httpport``.
DEFAULT_PUBLIC_HTTP_PORT = 9000

#: Where uvicorn's ASGI app listens; loopback only, never announced.
DEFAULT_INTERNAL_HTTP_PORT = 9001


def http_ports(cfg: "LyrionConfig | None" = None) -> tuple[int, int]:
    """Resolve the ``(public, internal)`` HTTP ports from the running config.

    Perl has a single HTTP port and serves EVERYTHING on it itself
    (``Slim/Web/HTTP.pm``), Bayeux included, because it supports pipelined
    requests (``Slim/Web/HTTP.pm:2065-2072``). The Python stack needs two
    processes for that: the native, pipelining-capable server is the FRONTEND
    on ``public_http_port`` (default 9000, bound on 0.0.0.0) and uvicorn moves
    to ``internal_http_port`` (default 9001, 127.0.0.1 only) behind it. Every
    announcement (TLV discovery, JSON presence beacon, ``serverstatus``'s
    ``httpport``) names the PUBLIC port, so the apps never see the internal
    one.

    Rollback needs no code revert: set ``public_http_port`` to 0 (or to the
    same value as ``internal_http_port``) and the frontend is not started,
    uvicorn binds the internal port itself — with both at 9000 the
    pre-consolidation layout is back.

    ``--httpport`` (the documented "HTTP port for web interface") still names
    the public port.
    """
    cfg = cfg or get_config()

    def _int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # CLI flags win (that is also how the rollback is switched off for a
    # single run); otherwise the stored pref, otherwise the default.
    cli = getattr(cfg, "cli_args", None)
    cli_public = getattr(cli, "public_http_port", None)
    if cli_public is None:
        cli_public = getattr(cli, "httpport", None)
    cli_internal = getattr(cli, "internal_http_port", None)

    internal = (_int(cli_internal, DEFAULT_INTERNAL_HTTP_PORT)
                if cli_internal is not None
                else _int(cfg.get("internal_http_port"),
                          DEFAULT_INTERNAL_HTTP_PORT))
    public = (_int(cli_public, DEFAULT_PUBLIC_HTTP_PORT)
              if cli_public is not None
              else _int(cfg.get("public_http_port"), DEFAULT_PUBLIC_HTTP_PORT))
    return public, internal


def public_http_port(cfg: "LyrionConfig | None" = None) -> int:
    """The single advertised HTTP port (see :func:`http_ports`)."""
    return http_ports(cfg)[0]


def frontend_enabled(cfg: "LyrionConfig | None" = None) -> bool:
    """True when the native frontend serves the public port.

    False means the single-port consolidation is rolled back: uvicorn binds
    the public port itself and no native streaming server is started.
    """
    public, internal = http_ports(cfg)
    return public > 0 and public != internal


def asgi_bind(cfg: "LyrionConfig | None" = None) -> tuple[str, int]:
    """``(host, port)`` uvicorn must bind.

    The ASGI app always listens on ``internal_http_port``; only its exposure
    differs. Normally the native frontend owns the public port and uvicorn is
    reachable on loopback alone. In the rollback layout (frontend disabled)
    uvicorn IS the public server again and binds 0.0.0.0 — with
    ``internal_http_port`` set back to 9000 that restores the old
    single-uvicorn-port setup without a code revert.
    """
    if frontend_enabled(cfg):
        return "127.0.0.1", http_ports(cfg)[1]
    return "0.0.0.0", http_ports(cfg)[1]
