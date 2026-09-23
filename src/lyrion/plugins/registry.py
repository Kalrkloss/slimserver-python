"""Manifest register (``install.xml``) for plugins.

Perl reference (``/tmp/lms-ref``, read-only)
============================================

``Slim::Utils::PluginManager`` reads one ``install.xml`` per plugin directory
below ``dirsFor('Plugins')`` and keeps the parsed manifests in a cache file:

* ``_findInstallManifests`` — walk the plugin dirs, keep every ``install.xml``,
  sum the ``mtime`` of all of them (``PluginManager.pm:631-654``).
* ``_parseInstallManifest`` — parse one file with ``XMLin``, derive the plugin
  name from the ``<module>`` (``Slim::Plugin::Name::Plugin`` / ``Plugins::Name::``)
  and fall back to the directory name, seed the pref state from
  ``<defaultState>``, record ``basedir``, grade the manifest (missing module,
  wrong version, wrong platform) and finally set ``error``
  (``PluginManager.pm:675-796``).
* ``_readInstallManifests`` — the loop over all files (``:656-673``).
* cache — ``plugin-data.yaml`` with ``__cacheinfo`` (version/bin/count/mtimesum/
  server/revision/osType/osArch); invalidated when the sum of the manifest mtimes
  changes (``_pluginCacheFile`` ``:586-590``, ``_writePluginCache`` ``:592-603``,
  ``_loadPluginCache`` ``:605-629``, validation ``:116-187``).

The port writes **JSON** instead of ``plugin-data.yaml`` (the task allows the
format change; the cache *semantics* — file name, invalidation by mtimesum —
stay Perl's).  ``CACHE_VERSION`` is Perl's ``4`` (``PluginManager.pm:41``).
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

#: Perl ``use constant CACHE_VERSION => 4`` (``PluginManager.pm:41``).
CACHE_VERSION = 4

#: Perl ``$plugins->{$name}->{error}`` values (``PluginManager.pm:239,734,738,783,791``).
INSTALLERROR_SUCCESS = "INSTALLERROR_SUCCESS"
INSTALLERROR_NO_MODULE = "INSTALLERROR_NO_MODULE"
INSTALLERROR_INVALID_VERSION = "INSTALLERROR_INVALID_VERSION"
INSTALLERROR_INCOMPATIBLE_PLATFORM = "INSTALLERROR_INCOMPATIBLE_PLATFORM"
INSTALLERROR_FAILED_TO_LOAD = "INSTALLERROR_FAILED_TO_LOAD"


@dataclass
class InstallManifest:
    """One parsed ``install.xml`` — Perl's ``$plugins->{$name}`` hash."""

    name: str                       #: plugin name (pref key)
    module: str = ""                #: ``<module>`` (Perl ``$manifest->{module}``)
    version: str = ""
    description: str = ""
    creator: str = ""
    category: str = ""
    plugin_type: str = ""           #: ``<type>`` (1 = skin, 2 = extension)
    default_state: str = ""
    enforce: bool = False           #: ``<enforce>`` (``PluginManager.pm:551,214,228``)
    basedir: str = ""               #: directory of the manifest (``:730``)
    #: ``<targetApplication><minVersion>/<maxVersion>`` (``:806-807``)
    min_version: str = ""
    max_version: str = ""
    error: str = ""
    #: Name of the extension repo the plugin came from (our cache only; Perl keeps
    #: this in the downloader's data, not in the manifest).
    title: str = ""
    icon: str = ""

    @property
    def manifest_path(self) -> Path:
        return Path(self.basedir) / "install.xml"

    @property
    def enabled_by_default(self) -> bool:
        """``defaultState`` = ``disabled`` → plugin starts out off.

        Perl: ``PluginManager.pm:711-728`` — the first time a plugin is seen the
        pref gets ``'disabled'`` if ``<defaultState>`` says so, else ``'enabled'``.
        A ``<defaultState>`` hash (per-OS) resolves to the OS entry or ``other``
        (``:715-718``); the port has no OS-hash form, the port's manifests carry
        the scalar form only.
        """
        return self.default_state.strip().lower() != "disabled"


_PLUGIN_NAME_RE_MODULE = re.compile(r"^Plugins::(.*)::")
_PLUGIN_NAME_RE_SLIM = re.compile(r"^Slim::Plugin::(.*)::")
_PLUGIN_NAME_RE_DIR = re.compile(r".*[\\/](.*)[\\/]install\.xml")


def plugin_name_from_manifest(module: str, path: Path) -> str:
    """Plugin name for a manifest — Perl ``_parseInstallManifest``:693-704.

    ``Slim::Plugin::DontStopTheMusic::Plugin`` → ``DontStopTheMusic``;
    ``Plugins::Foo::Plugin`` → ``Foo``; otherwise the directory name.
    """
    if module:
        match = _PLUGIN_NAME_RE_MODULE.match(module) or _PLUGIN_NAME_RE_SLIM.match(module)
        if match:
            return match.group(1)
    match = _PLUGIN_NAME_RE_DIR.match(str(path))
    return match.group(1) if match else path.parent.name


def _text(node: Optional[ET.Element]) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _bool_text(node: Optional[ET.Element]) -> bool:
    """``<enforce>`` truth test — Perl's scalar context (``PluginManager.pm:214,228,551``).

    Perl treats any non-empty value that is not ``0``/``false``/``no`` as true
    (XML::Simple turns ``<enforce>1</enforce>`` into the string ``"1"``).
    """
    if node is None:
        return False
    value = _text(node).lower()
    return value not in ("", "0", "false", "no")


class ManifestRegistry:
    """Reads ``install.xml`` manifests and caches them as JSON.

    Perl keeps this state in the package globals ``$plugins``/``$cacheInfo``
    (``PluginManager.pm:34,37``); the port keeps it in an instance so tests can
    point at a temporary plugin dir.
    """

    def __init__(self, plugin_dirs: Iterable[Path] | None = None) -> None:
        self.plugin_dirs: list[Path] = [Path(d) for d in (plugin_dirs or [])]
        self.manifests: dict[str, InstallManifest] = {}
        self.cache_info: dict = {}
        #: Set when the last :meth:`read` had to reparse (Perl ``$cacheInvalid``).
        self.cache_invalid_reason: str = ""

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def find_install_manifests(self) -> tuple[list[Path], int]:
        """All ``install.xml`` below the plugin dirs + their mtime sum.

        Perl ``_findInstallManifests`` (``PluginManager.pm:631-654``): depth-first
        over ``dirsFor('Plugins')``, ``file_filter`` keeps ``install.xml`` only,
        ``$mtimesum += (stat($file))[9]``.
        """
        files: list[Path] = []
        mtimesum = 0
        for base in self.plugin_dirs:
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("install.xml")):
                if not path.is_file():
                    continue
                try:
                    mtimesum += int(path.stat().st_mtime)
                except OSError:             # file vanished between rglob and stat
                    continue
                files.append(path)
        return files, mtimesum

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_manifest(self, path: Path) -> Optional[InstallManifest]:
        """Parse one ``install.xml`` — Perl ``_parseInstallManifest`` (``:675-796``).

        Grading order (Perl): missing module → ``INSTALLERROR_NO_MODULE``;
        ``targetApplication`` version mismatch → ``INSTALLERROR_INVALID_VERSION``
        (``_checkPluginVersion`` ``:798-820``); platform mismatch →
        ``INSTALLERROR_INCOMPATIBLE_PLATFORM`` (``:742-788``); else success.
        """
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ET.ParseError) as exc:
            logger.warning("Unable to parse XML in file [%s]: [%s]", path, exc)
            return None

        module = _text(root.find("module"))
        plugin_name = plugin_name_from_manifest(module, path)

        target = root.find("targetApplication")
        min_version = _text(target.find("minVersion")) if target is not None else ""
        max_version = _text(target.find("maxVersion")) if target is not None else ""

        manifest = InstallManifest(
            name=plugin_name,
            module=module,
            version=_text(root.find("version")),
            description=_text(root.find("description")),
            creator=_text(root.find("creator")),
            category=_text(root.find("category")),
            plugin_type=_text(root.find("type")),
            default_state=_text(root.find("defaultState")),
            enforce=_bool_text(root.find("enforce")),
            basedir=str(path.parent),
            min_version=min_version,
            max_version=max_version,
            title=_text(root.find("title")),
            icon=_text(root.find("icon")),
        )

        if not manifest.module:
            manifest.error = INSTALLERROR_NO_MODULE
        elif not check_plugin_version(manifest, server_version=self.server_version):
            manifest.error = INSTALLERROR_INVALID_VERSION
        else:
            manifest.error = INSTALLERROR_SUCCESS

        return manifest

    #: The version the manifests are graded against.  Perl compares
    #: ``$::VERSION`` (``PluginManager.pm:814``); the port announces the LMS
    #: version it emulates (``lyrion.__version__`` is the port's own version, so
    #: the compatible LMS version is what plugin manifests mean).
    server_version: str = "9.0.0"

    def read(self, *, use_cache: bool = True, cache_file: Optional[Path] = None,
             server_version: str = "9.0.0") -> dict[str, InstallManifest]:
        """(Re)read all manifests, using the JSON cache when it is still valid.

        Mirrors ``PluginManager.pm:116-187``: load the cache, compare the
        ``__cacheinfo`` fields (version/bin/count/mtimesum/server/revision/
        osType/osArch) and reparse when anything differs.
        """
        self.server_version = server_version
        files, mtimesum = self.find_install_manifests()

        self.cache_invalid_reason = ""
        cache_info: dict = {}
        if use_cache and cache_file is not None and cache_file.is_file():
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                cache_info = payload.get("__cacheinfo") or {}
                self.cache_invalid_reason = self._cache_invalid_reason(
                    cache_info, len(files), mtimesum, server_version)
                if not self.cache_invalid_reason:
                    self.manifests = {
                        name: _manifest_from_json(entry)
                        for name, entry in payload.items() if name != "__cacheinfo"
                    }
                    self.cache_info = cache_info
                    logger.info("Loaded plugin cache file (%d plugins).", len(self.manifests))
                    return self.manifests
            except (OSError, ValueError, TypeError) as exc:
                self.cache_invalid_reason = f"plugin cache unreadable: {exc}"
        elif not use_cache:
            self.cache_invalid_reason = "cache disabled by caller"
        else:
            self.cache_invalid_reason = "no plugin cache"

        logger.info("Reparsing plugin manifests - %s", self.cache_invalid_reason)
        self.manifests = {}
        for path in files:
            manifest = self.parse_manifest(path)
            if manifest is not None:
                self.manifests[manifest.name] = manifest

        self.cache_info = {
            "version": CACHE_VERSION,
            "count": len(files),
            "mtimesum": mtimesum,
            "server": server_version,
        }
        if use_cache and cache_file is not None:
            self.write_cache(cache_file)
        return self.manifests

    def _cache_invalid_reason(self, cache_info: dict, count: int, mtimesum: int,
                              server_version: str) -> str:
        """Perl's comparison chain (``PluginManager.pm:131-165``) — port subset.

        Kept: cache version, plugin count, manifest mtime sum, server version.
        Perl additionally compares ``$Bin``/``$::REVISION`` and the OS details;
        those have no port counterpart, the port's plugin dirs are fixed by the
        caller instead.
        """
        if not cache_info or not cache_info.get("version"):
            return "no plugin cache or cache disabled by caller"
        if cache_info.get("version") != CACHE_VERSION:
            return "cache version does not match"
        if cache_info.get("count") != count:
            return "different number of plugins in cache"
        if cache_info.get("mtimesum") != mtimesum:
            return "manifest checksum differs"
        if cache_info.get("server") != server_version:
            return "server version changed"
        return ""

    def write_cache(self, cache_file: Path) -> None:
        """``_writePluginCache`` (``PluginManager.pm:592-603``) as JSON."""
        payload: dict = {
            name: _manifest_to_json(manifest)
            for name, manifest in self.manifests.items()
        }
        payload["__cacheinfo"] = self.cache_info
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write plugin cache %s: %s", cache_file, exc)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[InstallManifest]:
        return self.manifests.get(name)

    def data_for_plugin(self, name: str) -> dict:
        """Perl ``dataForPlugin`` — the manifest as a plain dict."""
        manifest = self.manifests.get(name)
        if manifest is None:
            return {}
        return _manifest_to_json(manifest)

    def names(self) -> list[str]:
        return sorted(self.manifests)


# ── version comparison (Perl ``Slim::Utils::Versions->checkVersion``) ─────────


_VERSION_SPLIT_RE = re.compile(r"[._]")


def _version_parts(version: str) -> list[int]:
    """``9.0.0``/``7.9``/``7.0a`` → ``[9,0,0]`` — Perl ``checkVersion`` is a
    numeric compare after splitting on ``.`` and ``_`` (``Slim/Utils/Versions.pm``),
    with trailing letters dropped (``7.0a`` counts as ``7.0``).
    """
    parts: list[int] = []
    for token in _VERSION_SPLIT_RE.split(str(version).strip()):
        match = re.match(r"^(\d+)", token)
        parts.append(int(match.group(1)) if match else 0)
    return parts


def compare_versions(a: str, b: str) -> int:
    """``-1``/``0``/``1`` for ``a`` vs ``b`` (Perl ``compareVersions``)."""
    left, right = _version_parts(a), _version_parts(b)
    width = max(len(left), len(right))
    left += [0] * (width - len(left))
    right += [0] * (width - len(right))
    return (left > right) - (left < right)


def check_plugin_version(manifest: InstallManifest, *, server_version: str,
                         use_unsupported: bool = False) -> bool:
    """``_checkPluginVersion`` (``PluginManager.pm:798-820``).

    No ``targetApplication`` → incompatible (``:801-804``).  A ``maxVersion`` of
    ``*`` means "any" (``:807``); ``useUnsupported`` forces ``*`` (``:811``).
    """
    if not manifest.min_version and not manifest.max_version:
        return False
    minimum = manifest.min_version or "0"
    maximum = manifest.max_version or "*"
    if use_unsupported:
        maximum = "*"
    if compare_versions(server_version, minimum) < 0:
        return False
    if maximum != "*" and compare_versions(server_version, maximum) > 0:
        return False
    return True


# ── JSON mapping ────────────────────────────────────────────────────────────


def _manifest_to_json(manifest: InstallManifest) -> dict:
    return {
        "name": manifest.name,
        "module": manifest.module,
        "version": manifest.version,
        "description": manifest.description,
        "creator": manifest.creator,
        "category": manifest.category,
        "type": manifest.plugin_type,
        "defaultState": manifest.default_state,
        "enforce": manifest.enforce,
        "basedir": manifest.basedir,
        "minVersion": manifest.min_version,
        "maxVersion": manifest.max_version,
        "error": manifest.error,
        "title": manifest.title,
        "icon": manifest.icon,
    }


def _manifest_from_json(entry: dict) -> InstallManifest:
    return InstallManifest(
        name=entry.get("name", ""),
        module=entry.get("module", ""),
        version=entry.get("version", ""),
        description=entry.get("description", ""),
        creator=entry.get("creator", ""),
        category=entry.get("category", ""),
        plugin_type=entry.get("type", ""),
        default_state=entry.get("defaultState", ""),
        enforce=bool(entry.get("enforce")),
        basedir=entry.get("basedir", ""),
        min_version=entry.get("minVersion", ""),
        max_version=entry.get("maxVersion", ""),
        error=entry.get("error", ""),
        title=entry.get("title", ""),
        icon=entry.get("icon", ""),
    )
