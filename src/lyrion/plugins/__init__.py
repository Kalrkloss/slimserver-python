"""Plugin system for Pyrion Music Server.

Perl counterpart: ``Slim/Utils/PluginManager.pm`` plus one directory per plugin
under ``Slim/Plugin/``.  The port ships its plugins as Python modules inside this
package, each with a Perl-shaped ``install.xml`` in its own subdirectory
(``plugins/DontStopTheMusic/install.xml``) so the manifest register finds them
the same way it finds any other plugin.
"""

from .base import Plugin, PluginMetadata
from .manager import PluginManager, get_plugin_manager
from .registry import (
    CACHE_VERSION,
    INSTALLERROR_SUCCESS,
    InstallManifest,
    ManifestRegistry,
)

__all__ = [
    "Plugin",
    "PluginMetadata",
    "PluginManager",
    "get_plugin_manager",
    "InstallManifest",
    "ManifestRegistry",
    "CACHE_VERSION",
    "INSTALLERROR_SUCCESS",
]
