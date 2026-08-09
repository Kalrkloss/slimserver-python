"""Base plugin classes for Lyrion Music Server."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class PluginMetadata:
    """Metadata describing a plugin."""
    name: str
    version: str
    author: str = ""
    description: str = ""
    id: str = ""
    plugin_type: str = "general"
    dependencies: list[str] = field(default_factory=list)
    min_lms_version: str = "9.0.0"


class Plugin(ABC):
    """Abstract base class for Lyrion plugins.

    All plugins must inherit from this class and implement the required
    abstract methods. The metadata attribute must be set as a class attribute.
    """

    metadata: PluginMetadata
    enabled: bool = False
    hooks: dict[str, list[Callable]] = field(default_factory=dict)

    def __init__(self) -> None:
        self._logger = logging.getLogger(f"{__name__}.{self.metadata.id}")

    def get_metadata(self) -> PluginMetadata:
        """Return the plugin metadata."""
        return self.metadata

    def enable(self) -> None:
        """Enable the plugin. Called when the plugin is activated."""
        self.enabled = True
        self._logger.info("Plugin %s enabled", self.metadata.name)

    def disable(self) -> None:
        """Disable the plugin. Called when the plugin is deactivated."""
        self.enabled = False
        self._logger.info("Plugin %s disabled", self.metadata.name)

    def register_hook(self, name: str, callback: Callable) -> None:
        """Register a callback for a named hook.

        Args:
            name: The hook name (e.g. "track_change", "player_connect").
            callback: The callable to invoke when the hook fires.
        """
        if name not in self.hooks:
            self.hooks[name] = []
        self.hooks[name].append(callback)
        self._logger.debug("Registered hook '%s' on plugin %s", name, self.metadata.id)

    def unregister_hook(self, name: str, callback: Callable) -> None:
        """Remove a previously registered hook callback.

        Args:
            name: The hook name.
            callback: The previously registered callable.
        """
        if name in self.hooks and callback in self.hooks[name]:
            self.hooks[name].remove(callback)
            self._logger.debug(
                "Unregistered hook '%s' from plugin %s", name, self.metadata.id
            )

    async def on_startup(self) -> None:
        """Called once when the server starts and the plugin is enabled."""
        self._logger.debug("Plugin %s startup", self.metadata.id)

    async def on_shutdown(self) -> None:
        """Called once when the server is shutting down."""
        self._logger.debug("Plugin %s shutdown", self.metadata.id)

    async def on_track_change(self, track_id: int) -> None:
        """Called when the currently playing track changes.

        Args:
            track_id: The database ID of the new track.
        """
        self._logger.debug(
            "Plugin %s: track change to %s", self.metadata.id, track_id
        )

    async def on_player_connect(self, player_mac: str) -> None:
        """Called when a player connects to the server.

        Args:
            player_mac: The MAC address of the connecting player.
        """
        self._logger.debug(
            "Plugin %s: player connected %s", self.metadata.id, player_mac
        )

    async def on_player_disconnect(self, player_mac: str) -> None:
        """Called when a player disconnects from the server.

        Args:
            player_mac: The MAC address of the disconnecting player.
        """
        self._logger.debug(
            "Plugin %s: player disconnected %s", self.metadata.id, player_mac
        )

    async def on_cli_command(self, command: str, args: list) -> Optional[str]:
        """Handle a CLI command emitted by this plugin.

        Args:
            command: The command name (without leading prefix).
            args: List of arguments following the command.

        Returns:
            Response string, or None to fall through to the next handler.
        """
        return None

    async def on_web_request(
        self, path: str, method: str
    ) -> Optional[Any]:
        """Handle an incoming web request directed at this plugin.

        Args:
            path: The URL path after /plugin/<plugin_id>/.
            method: The HTTP method (GET, POST, etc.).

        Returns:
            A Response-compatible object, or None to fall through.
        """
        return None

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}("
            f"id={self.metadata.id!r}, enabled={self.enabled})>"
        )
