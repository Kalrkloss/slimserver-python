"""Plugin manager for Pyrion Music Server."""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

from .base import Plugin, PluginMetadata

logger = logging.getLogger(__name__)


class PluginManager:
    """Singleton plugin manager that discovers, loads, and coordinates plugins.

    The manager maintains a registry of loaded plugin instances and a global
    hook registry that allows plugins to subscribe to server events.
    """

    _instance: Optional[PluginManager] = None

    def __new__(cls) -> PluginManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        # Search paths for plugin discovery
        self.plugin_dirs: list[Path] = [
            Path.home() / ".lyrion" / "Plugins",
            Path("/opt/lyrion-plugins"),
            Path(__file__).parent.parent.parent.parent / "plugins",
        ]

        self.plugins: dict[str, Plugin] = {}
        self.enabled_plugins: set[str] = set()
        self.hooks: dict[str, list[tuple[str, Callable]]] = {}
        self._discovered: list[PluginMetadata] = []

        logger.info(
            "PluginManager initialized, search paths: %s", self.plugin_dirs
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_plugins(self) -> list[PluginMetadata]:
        """Scan plugin directories for available plugins.

        Returns:
            List of plugin metadata for all discoverable plugins.
        """
        self._discovered = []
        for plugin_dir in self.plugin_dirs:
            if not plugin_dir.is_dir():
                continue
            for entry in os.scandir(plugin_dir):
                if not entry.is_dir() and not entry.name.endswith(".py"):
                    continue
                metadata = self._scan_plugin_entry(entry)
                if metadata:
                    self._discovered.append(metadata)
                    logger.debug("Discovered plugin: %s v%s", metadata.name, metadata.version)

        logger.info("Discovered %d plugins", len(self._discovered))
        return self._discovered

    def _scan_plugin_entry(self, entry: os.DirEntry) -> Optional[PluginMetadata]:
        """Inspect a single plugin directory or file for metadata."""
        try:
            if entry.is_dir():
                module_path = Path(entry.path)
            else:
                module_path = Path(entry.path)

            spec = importlib.util.spec_from_file_location(
                "__lyrion_plugin__", module_path / "__init__.py"
                if module_path.is_dir() else entry.path
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                # Try to extract metadata without fully loading
                sys.modules["__lyrion_plugin__"] = module
                try:
                    spec.loader.exec_module(module)
                finally:
                    sys.modules.pop("__lyrion_plugin__", None)

                if hasattr(module, "get_metadata"):
                    return module.get_metadata()
                if hasattr(module, "PLUGIN_METADATA"):
                    return module.PLUGIN_METADATA
        except Exception as e:
            logger.warning("Failed to scan plugin entry %s: %s", entry.path, e)
        return None

    # ------------------------------------------------------------------
    # Loading / Unloading
    # ------------------------------------------------------------------

    def load_plugin(self, module_name: str) -> Plugin:
        """Dynamically load a plugin by module name.

        Args:
            module_name: Dotted module path, e.g. "mystuff.myplugin".

        Returns:
            The loaded Plugin instance.

        Raises:
            ImportError: If the module cannot be loaded.
        """
        if module_name in self.plugins:
            logger.debug("Plugin %s already loaded", module_name)
            return self.plugins[module_name]

        spec = importlib.util.find_spec(module_name)
        if spec is None:
            raise ImportError(f"No module named '{module_name}'")

        module = importlib.import_module(module_name)
        plugin: Optional[Plugin] = None

        if hasattr(module, "get_plugin"):
            plugin = module.get_plugin()
        elif hasattr(module, "Plugin"):
            plugin = module.Plugin()
        else:
            raise ImportError(
                f"Module '{module_name}' does not expose a Plugin class or get_plugin()"
            )

        self.plugins[plugin.metadata.id] = plugin
        logger.info("Loaded plugin: %s (%s)", plugin.metadata.name, plugin.metadata.id)
        return plugin

    def unload_plugin(self, plugin_id: str) -> None:
        """Unload a plugin by ID.

        Args:
            plugin_id: The plugin's unique identifier.
        """
        plugin = self.plugins.pop(plugin_id, None)
        if plugin is None:
            logger.warning("Plugin %s is not loaded", plugin_id)
            return

        self.enabled_plugins.discard(plugin_id)
        # Remove global hooks registered by this plugin
        for hook_name in list(self.hooks.keys()):
            self.hooks[hook_name] = [
                (pid, cb) for pid, cb in self.hooks[hook_name] if pid != plugin_id
            ]
            if not self.hooks[hook_name]:
                del self.hooks[hook_name]

        logger.info("Unloaded plugin: %s", plugin_id)

    def enable_plugin(self, plugin_id: str) -> None:
        """Enable a loaded plugin.

        Args:
            plugin_id: The plugin's unique identifier.
        """
        plugin = self.plugins.get(plugin_id)
        if plugin is None:
            logger.error("Cannot enable unknown plugin: %s", plugin_id)
            return
        if plugin.enabled:
            logger.debug("Plugin %s already enabled", plugin_id)
            return

        # Check dependencies
        for dep in plugin.metadata.dependencies:
            if dep not in self.enabled_plugins:
                logger.error(
                    "Plugin %s requires %s which is not enabled",
                    plugin_id, dep,
                )
                return

        plugin.enable()
        self.enabled_plugins.add(plugin_id)
        logger.info("Enabled plugin: %s", plugin_id)

    def disable_plugin(self, plugin_id: str) -> None:
        """Disable a loaded plugin.

        Args:
            plugin_id: The plugin's unique identifier.
        """
        plugin = self.plugins.get(plugin_id)
        if plugin is None:
            return
        plugin.disable()
        self.enabled_plugins.discard(plugin_id)
        logger.info("Disabled plugin: %s", plugin_id)

    def get_plugin(self, plugin_id: str) -> Optional[Plugin]:
        """Return a loaded plugin by ID, or None."""
        return self.plugins.get(plugin_id)

    # ------------------------------------------------------------------
    # Global hooks
    # ------------------------------------------------------------------

    def register_global_hook(
        self, name: str, callback: Callable, plugin_id: str
    ) -> None:
        """Register a global hook callback from a plugin.

        Args:
            name: Hook name (e.g. "track_change").
            callback: The callable to invoke.
            plugin_id: ID of the plugin owning this callback.
        """
        if name not in self.hooks:
            self.hooks[name] = []
        self.hooks[name].append((plugin_id, callback))
        logger.debug(
            "Registered global hook '%s' for plugin %s", name, plugin_id
        )

    def call_hooks(self, name: str, *args, **kwargs) -> list:
        """Invoke all callbacks registered for a hook.

        Args:
            name: Hook name.
            *args, **kwargs: Passed to each callback.

        Returns:
            List of non-None return values from callbacks.
        """
        results = []
        callbacks = self.hooks.get(name, [])
        for plugin_id, callback in callbacks:
            try:
                result = callback(*args, **kwargs)
                if result is not None:
                    results.append(result)
            except Exception as e:
                logger.error(
                    "Hook '%s' callback for plugin %s raised: %s",
                    name, plugin_id, e,
                )
        return results

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Discover and load all enabled plugins.

        Called once during server startup.
        """
        logger.info("PluginManager starting up...")
        self.discover_plugins()

        # Load discovered plugins
        for meta in self._discovered:
            try:
                self.load_plugin(f"lyrion.plugins.{meta.id}")
            except ImportError:
                # Try as third-party plugin
                for base in self.plugin_dirs:
                    try:
                        mod_path = str(base / meta.id)
                        spec = importlib.util.spec_from_file_location(
                            meta.id, base / meta.id / "__init__.py"
                        )
                        if spec:
                            module = importlib.module_from_spec(spec)
                            sys.modules[meta.id] = module
                            spec.loader.exec_module(module)
                            plugin: Plugin = module.Plugin()
                            self.plugins[plugin.metadata.id] = plugin
                    except Exception:
                        pass

        # Enable plugins that were previously enabled
        # (In production this would be read from preferences DB)
        for plugin_id in self.enabled_plugins:
            plugin = self.plugins.get(plugin_id)
            if plugin:
                plugin.enable()

        # Call startup hook on enabled plugins
        for plugin in list(self.plugins.values()):
            if plugin.enabled:
                await self._safe_plugin_call(plugin, "on_startup")

        logger.info(
            "PluginManager ready: %d loaded, %d enabled",
            len(self.plugins), len(self.enabled_plugins),
        )

    async def shutdown(self) -> None:
        """Shut down all plugins gracefully."""
        logger.info("PluginManager shutting down...")
        for plugin in list(self.plugins.values()):
            if plugin.enabled:
                await self._safe_plugin_call(plugin, "on_shutdown")

    async def _safe_plugin_call(
        self, plugin: Plugin, method_name: str, *args, **kwargs
    ):
        """Safely call a plugin method, catching all exceptions."""
        method = getattr(plugin, method_name, None)
        if method is None:
            return
        try:
            await method(*args, **kwargs)
        except Exception as e:
            logger.error(
                "Plugin %s.%s raised: %s",
                plugin.metadata.id, method_name, e,
            )
