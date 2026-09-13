"""Type definitions for the tool system."""

from enum import Enum

from dataclasses import dataclass
from typing import Any, Dict, Optional
from plugins_func.register import Action


class ToolType(Enum):
    """Tool type enum."""

    SERVER_PLUGIN = "server_plugin"  # server-side plugin
    SERVER_MCP = "server_mcp"  # server-side MCP
    DEVICE_IOT = "device_iot"  # device-side IoT
    DEVICE_MCP = "device_mcp"  # device-side MCP
    MCP_ENDPOINT = "mcp_endpoint"  # MCP endpoint


@dataclass
class ToolDefinition:
    """Tool definition."""

    name: str  # tool name
    description: Dict[str, Any]  # tool description (OpenAI function-calling format)
    tool_type: ToolType  # tool type
    parameters: Optional[Dict[str, Any]] = None  # extra parameters
