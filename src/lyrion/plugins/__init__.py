"""Plugin system for Lyrion Music Server."""
from .manager import PluginManager
from .base import Plugin, PluginMetadata

__all__ = ["PluginManager", "Plugin", "PluginMetadata"]
