"""Plugin system for Pyrion Music Server."""
from .manager import PluginManager
from .base import Plugin, PluginMetadata

__all__ = ["PluginManager", "Plugin", "PluginMetadata"]
