"""Tool and capability registries.

Two scopes, deliberately separate:

* :class:`RobotToolRegistry` — the tools of **one** robot, keyed by sanitized name.
* :class:`RobotCapabilityRegistry` — every connected robot's capabilities, the index the
  rest of the server asks "can robot X do Y?".

Capabilities are always discovered, never hardcoded: a robot with no entry here has not
finished discovery (or does not speak MCP), which is a state callers must handle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Protocol

from robot.state.models import RobotCapabilities, RobotTool


class ToolChannel(Protocol):
    """What the robot subsystem needs from a device tool channel.

    :class:`robot.devices.mcp.RobotToolClient` implements it over JSON-RPC MCP; the
    session adapter implements it over the inherited device-MCP client.
    """

    async def discover(self) -> RobotCapabilities: ...

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> str: ...

    async def aclose(self) -> None: ...


class RobotToolRegistry:
    """The tools of one robot. Not a concurrency boundary — the capability registry is."""

    def __init__(self, tools: Iterable[RobotTool] = ()) -> None:
        self._tools: dict[str, RobotTool] = {}
        for tool in tools:
            self._tools[tool.name] = tool

    def add(self, tool: RobotTool) -> bool:
        """Add a tool. Returns whether it was new (a redefinition replaces and returns False)."""
        is_new = tool.name not in self._tools
        self._tools[tool.name] = tool
        return is_new

    def replace(self, tools: Iterable[RobotTool]) -> tuple[RobotTool, ...]:
        """Swap in a freshly discovered tool set. Returns the tools that are new.

        Used on reconnect: firmware may have been updated between sessions, so the old
        set is discarded rather than merged.
        """
        incoming = {tool.name: tool for tool in tools}
        added = tuple(tool for name, tool in incoming.items() if name not in self._tools)
        self._tools = incoming
        return added

    def get(self, name: str) -> RobotTool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def list(self) -> tuple[RobotTool, ...]:
        return tuple(self._tools.values())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def clear(self) -> None:
        self._tools.clear()

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[RobotTool]:
        return iter(self._tools.values())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools


class RobotCapabilityRegistry:
    """Discovered capabilities, per robot. Safe under asyncio concurrency."""

    def __init__(self) -> None:
        self._capabilities: dict[str, RobotCapabilities] = {}
        self._tools: dict[str, RobotToolRegistry] = {}
        self._lock = asyncio.Lock()

    async def set(self, robot_id: str, capabilities: RobotCapabilities) -> tuple[RobotTool, ...]:
        """Record a discovery result. Returns the tools that were not there before."""
        async with self._lock:
            registry = self._tools.setdefault(robot_id, RobotToolRegistry())
            added = registry.replace(capabilities.tools)
            self._capabilities[robot_id] = capabilities
            return added

    async def get(self, robot_id: str) -> RobotCapabilities | None:
        async with self._lock:
            return self._capabilities.get(robot_id)

    async def tools(self, robot_id: str) -> RobotToolRegistry | None:
        async with self._lock:
            return self._tools.get(robot_id)

    async def has_tool(self, robot_id: str, name: str) -> bool:
        async with self._lock:
            registry = self._tools.get(robot_id)
            return registry is not None and registry.has(name)

    async def drop(self, robot_id: str) -> bool:
        async with self._lock:
            self._tools.pop(robot_id, None)
            return self._capabilities.pop(robot_id, None) is not None

    async def robot_ids(self) -> list[str]:
        async with self._lock:
            return list(self._capabilities)
