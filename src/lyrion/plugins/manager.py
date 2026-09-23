"""Plugin manager for Pyrion Music Server.

Perl reference (``/tmp/lms-ref``, read-only)
============================================

``Slim::Utils::PluginManager`` (884 lines) has three layers the port now
follows:

1. **Manifest register** — ``install.xml`` discovery, name derivation, grading
   and the JSON cache.  Ported into :mod:`lyrion.plugins.registry`
   (``PluginManager.pm:631-820``).
2. **State machine** — the pref store ``preferences('plugin.state')`` holds one
   entry per plugin: ``disabled``, ``enabled``, ``needs-enable``,
   ``needs-disable``, ``needs-install``, ``needs-uninstall``
   (``PluginManager.pm:31-32``).  ``init`` processes pending operations
   (``:88-114``), ``load`` skips what may not be loaded (``:190-242``), and
   ``_needsEnable``/``_needsDisable`` resolve the pending states to their final
   value (``:822-838``).  :meth:`PluginManager.resolve_pending_states` and
   :meth:`PluginManager.load` are the port.
3. **Load the modules** — Perl requires the module, records it in ``$loaded``
   and the module's ``preinitPlugin``/``initPlugin``/``postinitPlugin`` run in
   three passes (``:387-404``).  :meth:`PluginManager.startup` maps the three
   passes to ``on_preinit``/``on_startup``/``on_postinit``; the port ships its
   concrete plugins as Python modules instead of ``PAR``/``.pm`` files.

``enablePlugin``/``disablePlugin`` (``:535-563``) are the entry points the web
page and the CLI use: they only write the pending state, the next server start
resolves it — this is why the page shows ``PLUGINS_RESTART_MSG``
(``:569-585``).
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from .base import Plugin, PluginMetadata
from .registry import (
    INSTALLERROR_SUCCESS,
    ManifestRegistry,
)

logger = logging.getLogger(__name__)

#: The six states of Perl ``preferences('plugin.state')`` (``PluginManager.pm:32``).
STATE_DISABLED = "disabled"
STATE_ENABLED = "enabled"
STATE_NEEDS_ENABLE = "needs-enable"
STATE_NEEDS_DISABLE = "needs-disable"
STATE_NEEDS_INSTALL = "needs-install"
STATE_NEEDS_UNINSTALL = "needs-uninstall"

VALID_STATES = frozenset({
    STATE_DISABLED, STATE_ENABLED, STATE_NEEDS_ENABLE, STATE_NEEDS_DISABLE,
    STATE_NEEDS_INSTALL, STATE_NEEDS_UNINSTALL,
})

#: Server-wide pref that holds the per-plugin state (Perl
#: ``preferences('plugin.state')``, ``PluginManager.pm:31``).  Our store is flat,
#: so the keys are ``plugin.state:<plugin name>``.
PREF_NAMESPACE = "plugin.state"
PREF_KEY_TEMPLATE = PREF_NAMESPACE + ":{plugin}"

#: ``PLUGIN_RANDOM``/``RandomPlay`` module names this port knows as the
#: provider namespace of DSTM.
RANDOMPLAY_PLUGIN_ID = "RandomPlay"

#: Shipped plugins: Perl plugin name (``install.xml`` ``<name>`` directory /
#: ``<module>`` part) → Python module under ``lyrion.plugins``.  Perl's name is
#: CamelCase without word breaks, so the mapping is explicit rather than derived.
SHIPPED_PLUGIN_MODULES: dict[str, str] = {
    "DontStopTheMusic": "lyrion.plugins.dontstopthemusic",
}


def _state_key(plugin: str) -> str:
    return PREF_KEY_TEMPLATE.format(plugin=plugin)


class PluginManager:
    """Discovers, loads and coordinates plugins.

    The manager keeps a registry of loaded plugin instances and a global hook
    registry that lets plugins subscribe to server events.
    """

    _instance: Optional[PluginManager] = None

    def __new__(cls) -> PluginManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        #: Where plugin modules live.  Metric: this is the ``Plugins`` dir of
        #: Perl (``Slim::Utils::OSDetect::dirsFor('Plugins')``,
        #: ``PluginManager.pm:645``); the port's shipped plugins live in this
        #: package (``src/lyrion/plugins``), third-party ones below
        #: ``<serverdata>/Plugins``.
        self.package_dir = Path(__file__).parent
        self.plugin_dirs: list[Path] = [self.package_dir]

        self.registry = ManifestRegistry(self.plugin_dirs)
        self.plugins: dict[str, Plugin] = {}
        #: Perl ``$loaded`` keyed by module name (``PluginManager.pm:35``).
        self.loaded: dict[str, Plugin] = {}
        #: Names of the enabled plugins — Perl ``enabledPlugins``.
        self.enabled_plugin_names: set[str] = set()
        self.hooks: dict[str, list[tuple[str, Callable]]] = {}
        #: Plugins that failed to load — the port's ``$disabled`` map
        #: (``PluginManager.pm:36``).
        self.disabled: set[str] = set()
        self._playlist_change_hooks: list[Callable[[dict], Awaitable[None]]] = []
        self._started = False

        logger.info("PluginManager initialized, search paths: %s", self.plugin_dirs)

    # ------------------------------------------------------------------
    # Registration of extra search paths / server data dir
    # ------------------------------------------------------------------

    def add_plugin_dir(self, directory: Path | str) -> None:
        """Register another plugin directory (Perl's ``dirsFor('Plugins')``)."""
        path = Path(directory)
        if path not in self.plugin_dirs:
            self.plugin_dirs.append(path)
            self.registry.plugin_dirs = self.plugin_dirs

    def cache_file(self) -> Path:
        """``_pluginCacheFile`` (``PluginManager.pm:586-590``) as ``.json``."""
        try:
            from lyrion.config import get_config

            cache_dir = get_config().cache_dir
        except Exception:  # noqa: BLE001 — tests without a config
            cache_dir = Path.home() / ".lyrion" / "cache"
        return Path(cache_dir) / "plugin-data.json"

    # ------------------------------------------------------------------
    # Manifest access — Perl ``dataForPlugin``/``enabledPlugins``
    # ------------------------------------------------------------------

    def discover_plugins(self, *, use_cache: bool = True) -> list:
        """Read all manifests (``_readInstallManifests``, ``:656-673``).

        Also seeds the pref state of every newly seen plugin
        (``_parseInstallManifest`` ``:711-728``).  Returns the manifests.
        """
        self.registry.read(use_cache=use_cache, cache_file=self.cache_file())
        return list(self.registry.manifests.values())

    def data_for_plugin(self, name: str) -> dict:
        """Perl ``dataForPlugin`` (manifest hash of a plugin)."""
        return self.registry.data_for_plugin(name)

    def manifest_names(self) -> list[str]:
        return self.registry.names()

    def get_metadata(self, name: str) -> Optional[PluginMetadata]:
        manifest = self.registry.get(name)
        if manifest is None:
            return None
        return PluginMetadata(
            name=manifest.name,
            version=manifest.version,
            author=manifest.creator,
            description=manifest.description,
            id=manifest.name,
            plugin_type=manifest.plugin_type,
            min_lms_version=manifest.min_version,
        )

    # ------------------------------------------------------------------
    # Pref state machine — Perl ``enablePlugin``/``disablePlugin`` (:535-563)
    # ------------------------------------------------------------------

    def plugin_state(self, name: str) -> str:
        """Current state value of a plugin (``$prefs->get($name)``)."""
        from lyrion.config import get_prefs

        return str(get_prefs().get(_state_key(name), "") or "")

    async def seed_plugin_state(self, name: str, state: str) -> None:
        """Write a state, registering the pref first (Perl ``$prefs->set``)."""
        from lyrion.config import get_prefs

        prefs = get_prefs()
        key = _state_key(name)
        await prefs.init_preference(key, default="", category=PREF_NAMESPACE)
        await prefs.set(key, state)

    async def ensure_plugin_state(self, name: str, default_state: str) -> str:
        """Seed ``defaultState`` on first sight (``PluginManager.pm:711-728``).

        Perl sets ``'disabled'`` when ``<defaultState>`` is ``disabled`` and
        ``'enabled'`` otherwise.
        """
        current = self.plugin_state(name)
        if current:
            return current
        state = (STATE_DISABLED if str(default_state).strip().lower() == "disabled"
                 else STATE_ENABLED)
        await self.seed_plugin_state(name, state)
        return state

    async def enable_plugin(self, name: str) -> bool:
        """``enablePlugin`` (``PluginManager.pm:535-545``).

        Writes ``needs-enable`` unless the plugin is already ``enabled``.
        Returns whether a change was written.
        """
        current = self.plugin_state(name)
        if current == STATE_ENABLED:
            return False
        logger.info("Setting plugin %s to state: needs-enable", name)
        await self.seed_plugin_state(name, STATE_NEEDS_ENABLE)
        return True

    async def disable_plugin(self, name: str) -> bool:
        """``disablePlugin`` (``PluginManager.pm:547-563``).

        An ``enforce`` plugin cannot be disabled (``:551-556``); otherwise writes
        ``needs-disable`` unless the plugin is already ``disabled``.
        """
        manifest = self.registry.get(name)
        if manifest is not None and manifest.enforce:
            logger.warning(
                "Can't disable plugin: %s - 'enforce' set in install.xml", name)
            return False
        current = self.plugin_state(name)
        if current == STATE_DISABLED:
            return False
        logger.info("Setting plugin %s to state: needs-disable", name)
        await self.seed_plugin_state(name, STATE_NEEDS_DISABLE)
        return True

    def is_configured_enabled(self, name: str) -> bool:
        """``isConfiguredEnabled`` (``PluginManager.pm:528-533``)."""
        state = self.plugin_state(name)
        return bool(state) and state in (STATE_NEEDS_ENABLE, STATE_ENABLED)

    def is_enabled(self, module_or_name: str) -> bool:
        """``isEnabled`` (``PluginManager.pm:520-526``) — loaded and running.

        Perl keys ``$loaded`` by module name; the port accepts both the module
        path (``lyrion.plugins.dontstopthemusic``), the plugin name
        (``DontStopTheMusic``) and the Perl module (``Slim::Plugin::…::Plugin``).
        """
        key = module_or_name
        if key in self.loaded:
            return True
        tail = key.rsplit(".", 1)[-1]
        if tail in self.loaded or tail in self.plugins:
            return True
        if "::" in key:
            name = key.rsplit("::", 2)[-2] if key.count("::") >= 2 else key
            if name in self.plugins:
                return True
        return False

    def enabled_plugins(self) -> list[str]:
        """Perl ``enabledPlugins`` — plugin names of the loaded modules."""
        return sorted(self.plugins)

    async def resolve_pending_states(self) -> dict[str, str]:
        """Process the pending operations — ``PluginManager.pm:88-114``.

        ``needs-enable`` → ``enabled`` (``_needsEnable`` ``:822-829``),
        ``needs-disable`` → ``disabled`` (``_needsDisable`` ``:831-838``).
        ``needs-uninstall``/``needs-install`` belong to the downloader, which is
        out of scope for this task (A1 without download); they are logged and
        left alone so the download task can pick them up.
        """
        from lyrion.config import get_prefs

        resolved: dict[str, str] = {}
        for key in list(get_prefs().keys()):
            if not key.startswith(PREF_NAMESPACE + ":"):
                continue
            name = key[len(PREF_NAMESPACE) + 1:]
            state = self.plugin_state(name)
            if state == STATE_NEEDS_ENABLE:
                await self.seed_plugin_state(name, STATE_ENABLED)
                resolved[name] = STATE_ENABLED
                logger.info("enabling %s", name)
            elif state == STATE_NEEDS_DISABLE:
                await self.seed_plugin_state(name, STATE_DISABLED)
                resolved[name] = STATE_DISABLED
                logger.info("disabling %s", name)
            elif state in (STATE_NEEDS_INSTALL, STATE_NEEDS_UNINSTALL):
                logger.warning(
                    "plugin %s wants %s - the downloader is not part of this task",
                    name, state)
        return resolved

    def needs_restart(self) -> bool:
        """``needsRestart`` (``PluginManager.pm:565-567``) — any state with ``needs``."""
        return any(self.plugin_state(name).startswith("needs")
                   for name in self._state_names())

    def message(self, default: Optional[str] = None) -> Optional[str]:
        """``message`` (``PluginManager.pm:569-585``).

        With a pending restart: ``PLUGINS_RESTART_MSG`` followed by the localized
        names of the plugins waiting, in parentheses.  Otherwise the fallback.
        """
        if not self.needs_restart():
            return default
        from lyrion.i18n import get_string, resolve_language

        lang = resolve_language()
        waiting = [
            get_string(self.registry.get(name).name if self.registry.get(name) else name,
                       lang, default=name)
            for name in sorted(self._state_names())
            if self.plugin_state(name).startswith("needs")
        ]
        token = get_string("PLUGINS_RESTART_MSG", lang, default="PLUGINS_RESTART_MSG")
        return f"{token} ({', '.join(waiting)})"

    def _state_names(self) -> list[str]:
        from lyrion.config import get_prefs

        return [key[len(PREF_NAMESPACE) + 1:] for key in get_prefs().keys()
                if key.startswith(PREF_NAMESPACE + ":")]

    # ------------------------------------------------------------------
    # Loading — Perl ``load`` (``PluginManager.pm:190-410``)
    # ------------------------------------------------------------------

    async def load(self) -> dict[str, Plugin]:
        """Load every plugin whose state allows it.

        The skip rules of ``PluginManager.pm:197-242`` in order: no module
        (``:203-204``), state not in the valid set (``:221-224``), state
        ``disabled`` — unless ``enforce`` re-enables it (``:226-236``, bug 17647),
        manifest error (``:239-242``).
        """
        self.plugins = {}
        self.loaded = {}
        self.disabled = set()
        self.enabled_plugin_names = set()

        for name in self.registry.names():
            manifest = self.registry.get(name)
            if manifest is None:
                continue
            state = await self.ensure_plugin_state(name, manifest.default_state)

            # Skip plugins with no module (:203-204).
            if not manifest.module:
                self.disabled.add(name)
                continue

            # State not valid (:221-224).
            if state not in VALID_STATES:
                logger.error("Skipping plugin: %s - in erroneous state: %s", name, state)
                self.disabled.add(name)
                continue

            if state == STATE_NEEDS_DISABLE:
                # (see resolve_pending_states — a leftover pending state)
                logger.warning("Skipping plugin: %s - pending disable", name)
                self.disabled.add(name)
                continue

            if state == STATE_DISABLED:
                if manifest.enforce:                    # :226-236
                    logger.warning("Re-enabling plugin as it is enforced: %s", name)
                    await self.seed_plugin_state(name, STATE_ENABLED)
                else:
                    logger.warning("Skipping plugin: %s - disabled", name)
                    self.disabled.add(name)
                    continue

            if manifest.error != INSTALLERROR_SUCCESS:  # :239-242
                logger.error("Couldn't load %s. Error: %s", name, manifest.error)
                self.disabled.add(name)
                continue

            logger.info("Loading plugin: %s", name)
            try:
                plugin = self._load_plugin_module(manifest)
            except Exception as exc:  # noqa: BLE001 — Perl logs and continues
                logger.error("Couldn't load %s: %s", manifest.module, exc)
                manifest.error = "INSTALLERROR_FAILED_TO_LOAD"
                self.disabled.add(name)
                continue
            if plugin is None:
                manifest.error = "INSTALLERROR_FAILED_TO_LOAD"
                self.disabled.add(name)
                continue

            self.plugins[name] = plugin
            self.loaded[manifest.module] = plugin
            self.loaded[name] = plugin
            self.enabled_plugin_names.add(name)

        return self.plugins

    def _load_plugin_module(self, manifest) -> Optional[Plugin]:
        """Import the plugin's Python module and build its instance.

        The port maps Perl's module name to a module path.  Shipped plugins live
        in ``lyrion.plugins.<module>`` and are found through
        :data:`SHIPPED_PLUGIN_MODULES` (explicit, because Perl's CamelCase names
        do not split into the module file names cleanly — ``DontStopTheMusic`` →
        ``dontstopthemusic``); anything else is tried as the dotted
        ``<module>`` itself, then by plugin name.
        """
        module_name = manifest.module
        candidates: list[str] = []
        shipped = SHIPPED_PLUGIN_MODULES.get(manifest.name)
        if shipped:
            candidates.append(shipped)
        if module_name.startswith("lyrion.") or ("." in module_name and "::" not in module_name):
            candidates.append(module_name)
        if manifest.name:
            candidates.append(f"lyrion.plugins.{manifest.name.lower()}")
            candidates.append(f"lyrion.plugins.{_snake(manifest.name)}")

        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                module = importlib.import_module(candidate)
            except ImportError:
                continue
            plugin = self._plugin_from_module(module, manifest.name)
            if plugin is not None:
                return plugin
        return None

    @staticmethod
    def _plugin_from_module(module: Any, name: str) -> Optional[Plugin]:
        """``get_plugin()`` first, then a ``Plugin`` class (see ``base.Plugin``)."""
        factory = getattr(module, "get_plugin", None)
        if callable(factory):
            plugin = factory()
            return plugin if isinstance(plugin, Plugin) else None
        cls = getattr(module, "Plugin", None)
        if cls is None and name:
            cls = getattr(module, _class_name(name), None)
        if cls is None or not isinstance(cls, type):
            return None
        plugin = cls()
        return plugin if isinstance(plugin, Plugin) else None

    # ------------------------------------------------------------------
    # Hook registry
    # ------------------------------------------------------------------

    def register_global_hook(self, name: str, callback: Callable, plugin_id: str) -> None:
        self.hooks.setdefault(name, []).append((plugin_id, callback))
        logger.debug("Registered global hook '%s' for plugin %s", name, plugin_id)

    async def call_hooks(self, name: str, *args, **kwargs) -> list:
        """Invoke all callbacks for a hook; async results are awaited."""
        results: list = []
        for plugin_id, callback in self.hooks.get(name, []):
            try:
                result = callback(*args, **kwargs)
                if hasattr(result, "__await__"):
                    result = await result
                if result is not None:
                    results.append(result)
            except Exception as exc:  # noqa: BLE001 — one plugin must not break others
                logger.error("Hook '%s' callback for plugin %s raised: %s",
                             name, plugin_id, exc)
        return results

    # ------------------------------------------------------------------
    # Playlist-change hook (Perl ``Slim::Control::Request::subscribe``)
    # ------------------------------------------------------------------

    def register_playlist_change_hook(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        """``Request::subscribe(\\&onPlaylistChange, [['playlist'], …])``
        (``Plugin.pm:91``)."""
        if callback not in self._playlist_change_hooks:
            self._playlist_change_hooks.append(callback)

    def unregister_playlist_change_hook(self, callback: Callable) -> None:
        if callback in self._playlist_change_hooks:
            self._playlist_change_hooks.remove(callback)

    async def notify_playlist_change(self, event: dict) -> None:
        """Fire a playlist event to every subscribed plugin.

        Called by the player manager on ``newsong``/``delete``/``cant_open``/
        ``resume`` (Perl ``Slim::Control::Request`` dispatch).
        """
        for callback in list(self._playlist_change_hooks):
            try:
                await callback(event)
            except Exception as exc:  # noqa: BLE001
                logger.error("playlist-change hook raised: %s", exc)

    # ------------------------------------------------------------------
    # Lifecycle — Perl ``load()`` init pass order (``:383-404``)
    # ------------------------------------------------------------------

    async def startup(self, *, use_cache: bool = True) -> None:
        """Discover, load and initialize all plugins (``PluginManager.pm:383-404``).

        Perl runs ``preinitPlugin``, ``initPlugin``, ``postinitPlugin`` in three
        sorted passes so plugins can offer services to each other; the port's
        counterparts are ``on_preinit``, ``on_startup``, ``on_postinit``.
        """
        logger.info("PluginManager starting up...")
        self.discover_plugins(use_cache=use_cache)
        await self.resolve_pending_states()
        await self.load()

        for method_name in ("on_preinit", "on_startup", "on_postinit"):
            for name in sorted(self.plugins):
                await self._safe_plugin_call(self.plugins[name], method_name)

        self._started = True
        logger.info("PluginManager ready: %d loaded, %d enabled",
                    len(self.plugins), len(self.enabled_plugin_names))

    async def shutdown(self) -> None:
        """``shutdownPlugins`` (``PluginManager.pm:412-428``)."""
        logger.info("Shutting down plugins...")
        for name in sorted(self.plugins):
            await self._safe_plugin_call(self.plugins[name], "on_shutdown")
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    async def _safe_plugin_call(self, plugin: Plugin, method_name: str, *args, **kwargs) -> None:
        """Call a plugin method, catching and logging exceptions (Perl evals)."""
        method = getattr(plugin, method_name, None)
        if method is None:
            return
        try:
            result = method(*args, **kwargs)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.error("Plugin %s.%s raised: %s",
                         plugin.metadata.id, method_name, exc)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_plugin(self, plugin_id: str) -> Optional[Plugin]:
        """Return a loaded plugin by plugin name or module path."""
        if plugin_id in self.plugins:
            return self.plugins[plugin_id]
        if plugin_id in self.loaded:
            return self.loaded[plugin_id]
        tail = plugin_id.rsplit(".", 1)[-1]
        if tail in self.plugins:
            return self.plugins[tail]
        return None

    def unload_plugin(self, plugin_id: str) -> None:
        plugin = self.get_plugin(plugin_id)
        if plugin is None:
            logger.warning("Plugin %s is not loaded", plugin_id)
            return
        for name in [n for n, pl in self.plugins.items() if pl is plugin]:
            self.plugins.pop(name, None)
            self.loaded.pop(name, None)
            self.enabled_plugin_names.discard(name)
        for module, pl in [kv for kv in self.loaded.items() if kv[1] is plugin]:
            self.loaded.pop(module, None)
        for hook_name in list(self.hooks):
            self.hooks[hook_name] = [(pid, cb) for pid, cb in self.hooks[hook_name]
                                     if cb not in _plugin_callbacks(plugin)]
            if not self.hooks[hook_name]:
                del self.hooks[hook_name]
        logger.info("Unloaded plugin: %s", plugin_id)

    def get_plugin_object(self, plugin_id: str) -> Optional[Plugin]:
        return self.get_plugin(plugin_id)


def _plugin_callbacks(plugin: Plugin) -> list:
    callbacks: list = []
    for entries in getattr(plugin, "hooks", {}) or {}:
        entries = getattr(plugin, "hooks", {}).get(entries, [])
        callbacks.extend(entries)
    return callbacks


def _snake(name: str) -> str:
    """``DontStopTheMusic`` → ``dontstopthemusic`` (import-safe module name)."""
    out = []
    for index, char in enumerate(name):
        if char.isupper() and index and not name[index - 1].isupper():
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def _class_name(name: str) -> str:
    return name


def get_plugin_manager() -> PluginManager:
    """Module-level accessor used by the startup wiring."""
    return PluginManager()


def registered_plugin_classes() -> Iterable[type[Plugin]]:
    return ()
