"""Device-facing robot code: the registry, the capability index and the tool channel.

This package is the only robot layer allowed to know that a device exists. It may import
``core/`` **lazily** (never at module scope) — see docs/robot-architecture.md Sect. 7.
"""

from robot.devices.mcp import (
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_ID_BASE,
    DEFAULT_REQUEST_TIMEOUT,
    MAX_TOOL_PAGES,
    MalformedToolError,
    McpError,
    McpProtocolError,
    McpTimeoutError,
    McpToolError,
    McpTransport,
    McpUnsupportedError,
    RobotToolClient,
    sanitize_tool_name,
    supports_mcp,
    tool_from_mcp,
    unwrap_tool_result,
)
from robot.devices.registry import RobotRegistry
from robot.devices.tools import RobotCapabilityRegistry, RobotToolRegistry, ToolChannel

__all__ = [
    "DEFAULT_CALL_TIMEOUT",
    "DEFAULT_ID_BASE",
    "DEFAULT_REQUEST_TIMEOUT",
    "MAX_TOOL_PAGES",
    "MalformedToolError",
    "McpError",
    "McpProtocolError",
    "McpTimeoutError",
    "McpToolError",
    "McpTransport",
    "McpUnsupportedError",
    "RobotCapabilityRegistry",
    "RobotRegistry",
    "RobotToolClient",
    "RobotToolRegistry",
    "ToolChannel",
    "sanitize_tool_name",
    "supports_mcp",
    "tool_from_mcp",
    "unwrap_tool_result",
]
