"""
Backward-compatibility module - plugins_func.register

Kept for backward compatibility; everything is re-exported from plugins.register
"""

from plugins.register import (
    register_function,
    register_device_function,
    ToolType,
    Action,
    ActionResponse,
    FunctionItem,
    DeviceTypeRegistry,
    FunctionRegistry,
    all_function_registry,
    module_func_map,
)

__all__ = [
    "register_function",
    "register_device_function",
    "ToolType",
    "Action",
    "ActionResponse",
    "FunctionItem",
    "DeviceTypeRegistry",
    "FunctionRegistry",
    "all_function_registry",
    "module_func_map",
]
