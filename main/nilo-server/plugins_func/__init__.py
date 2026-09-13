"""
Backward-compatibility shim - plugins_func package

Lets legacy code keep importing from the plugins_func path.
The actual implementation has moved to the plugins package.
"""

# Re-export everything from the new location
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
]
