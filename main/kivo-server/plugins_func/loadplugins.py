"""
Backward-compatibility module - plugins_func.loadplugins

This module exists for backward compatibility; the real implementation is imported from plugins.loadplugins
"""

from plugins.loadplugins import auto_import_modules

__all__ = ["auto_import_modules"]
