"""
Unified plugin system

This module provides a single plugin scanning and registration mechanism that supports:
1. Dynamic interceptor plugins (BasePlugin subclasses)
2. MCP function plugins (@register_function decorator)
3. Plugin subdirectory layout (config files and helper modules)
"""

import os
import sys
import importlib
import importlib.util
from typing import Optional

# Minimal logger so this module does not depend on loguru
class SimpleLogger:
    def __init__(self, tag):
        self.tag = tag

    def info(self, msg):
        print(f"[INFO] [{self.tag}] {msg}")

    def warning(self, msg):
        print(f"[WARNING] [{self.tag}] {msg}")

    def error(self, msg):
        print(f"[ERROR] [{self.tag}] {msg}")

    def bind(self, tag):
        return SimpleLogger(tag)

TAG = __name__
logger = SimpleLogger(TAG)

# Re-export common classes and functions for plugin authors
from .base import BasePlugin, PluginAction
from .register import register_function, ToolType, ActionResponse, Action
from .manager import PluginManager

__all__ = [
    "BasePlugin",
    "PluginAction",
    "register_function",
    "ToolType",
    "ActionResponse",
    "Action",
    "PluginManager",
    "scan_plugins",
    "register_plugins_to_conn",
]


def _is_plugin_module(dirname: str) -> bool:
    """Return True if the directory is a plugin module."""
    # Skip special directories
    if dirname.startswith("_") or dirname.startswith("."):
        return False
    # Skip known non-plugin directories
    if dirname in ["__pycache__", "functions"]:
        return False
    return True


def _scan_plugin_directory(plugin_root: str) -> list:
    """
    Scan the plugin directory and return all module paths to import.

    Args:
        plugin_root: path of the plugin root directory

    Returns:
        List of module paths, e.g. ['plugins.preprocess_plugin', 'plugins.my_plugin']
    """
    modules_to_import = []

    if not os.path.exists(plugin_root):
        return modules_to_import

    # Walk the plugin root directory
    for item in os.listdir(plugin_root):
        item_path = os.path.join(plugin_root, item)

        # Only handle directories
        if not os.path.isdir(item_path):
            continue

        if not _is_plugin_module(item):
            continue

        # Check whether the directory has an __init__.py
        init_file = os.path.join(item_path, "__init__.py")
        if os.path.exists(init_file):
            module_name = f"plugins.{item}"
            modules_to_import.append(module_name)

    return modules_to_import


def _import_module_safe(module_name: str) -> bool:
    """Import a module, swallowing errors."""
    try:
        importlib.import_module(module_name)
        logger.bind(tag=TAG).info(f"Loaded plugin module: {module_name}")
        return True
    except Exception as e:
        logger.bind(tag=TAG).warning(f"Failed to load plugin module {module_name}: {e}")
        return False


def scan_plugins() -> list:
    """
    Scan and import every plugin under the plugins/ directory.

    This function:
    1. Scans all subdirectories under plugins/
    2. Imports each subdirectory's __init__.py
    3. Triggers @register_function decorator registration
    4. Triggers BasePlugin subclass definitions (registered later via register_plugins_to_conn)

    Returns:
        List of successfully loaded modules
    """
    # Locate the plugins directory
    plugins_dir = os.path.dirname(os.path.abspath(__file__))

    # Scan the plugin directory
    modules = _scan_plugin_directory(plugins_dir)

    # Import all plugin modules
    loaded_modules = []
    for module_name in modules:
        if _import_module_safe(module_name):
            loaded_modules.append(module_name)

    # Backward compatibility with the old flat functions directory
    functions_dir = os.path.join(plugins_dir, "functions")
    if os.path.exists(functions_dir):
        # Import functions using the loadplugins logic
        try:
            from . import loadplugins
            loadplugins.auto_import_modules("plugins.functions")
            logger.bind(tag=TAG).info("Loaded MCP functions from the functions directory")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"Failed to load functions directory: {e}")

    logger.bind(tag=TAG).info(f"Plugin scan complete, loaded {len(loaded_modules)} plugin module(s)")
    return loaded_modules


def register_plugins_to_conn(conn):
    """
    Register the scanned plugins with the connection's plugin_manager.

    Call this when the connection is initialised.

    Args:
        conn: ConnectionHandler instance
    """
    if not hasattr(conn, 'plugin_manager'):
        logger.bind(tag=TAG).warning("Connection object has no plugin_manager attribute")
        return

    # Collect all imported modules

    # Find loaded modules in the plugins package
    plugins_modules = [
        name for name in sys.modules.keys()
        if name.startswith("plugins.") and not name.startswith("plugins.functions")
    ]

    registered_count = 0

    for module_name in plugins_modules:
        try:
            module = sys.modules.get(module_name)
            if not module:
                continue

            # Look for BasePlugin subclasses in the module
            for attr_name in dir(module):
                attr = getattr(module, attr_name)

                # Must be a BasePlugin subclass, but not BasePlugin itself
                if (isinstance(attr, type) and
                    issubclass(attr, BasePlugin) and
                    attr != BasePlugin):

                    # Instantiate and register
                    try:
                        plugin_instance = attr(logger=conn.logger if hasattr(conn, 'logger') else None)
                        conn.plugin_manager.register_plugin(plugin_instance)
                        logger.bind(tag=TAG).info(
                            f"Registered interceptor plugin: {plugin_instance.name} (from {module_name})"
                        )
                        registered_count += 1
                    except Exception as e:
                        logger.bind(tag=TAG).error(
                            f"Failed to register plugin {attr_name}: {e}"
                        )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error while processing module {module_name}: {e}")

    logger.bind(tag=TAG).info(f"Plugin registration complete, registered {registered_count} interceptor plugin(s)")


# Backward compatibility: import classes from the old path
try:
    from .register import all_function_registry
    __all__.append("all_function_registry")
except ImportError:
    pass
