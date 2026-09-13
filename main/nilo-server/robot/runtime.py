"""The robot control plane: one object that owns the subsystem for a process.

It wires the pieces together and owns the connection lifecycle:

    attach(device)  ->  register  ->  discover capabilities (off the read loop)
    detach(device)  ->  unregister, cancel discovery, close the tool channel

Nothing here is a singleton by construction: :class:`RobotRuntime` is built per process
by :func:`get_runtime` and per test by calling it directly, so two tests never share a
registry, a bus or a state store.

Discovery deliberately runs in its own task. The inherited read loop awaits every message
handler inline (robot-architecture R3), so anything that waits on a device would stall
ingestion of all later frames, audio included.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from robot.devices.mcp import McpError, McpUnsupportedError
from robot.devices.registry import RobotRegistry
from robot.devices.tools import RobotCapabilityRegistry, ToolChannel
from robot.events.bus import EventBus
from robot.events.types import ToolCallCompleted, ToolCallFailed, ToolCallStarted
from robot.state.models import (
    DeviceInfo,
    DisconnectReason,
    RobotCapabilities,
    RobotState,
    RobotTelemetry,
)
from robot.state.store import InMemoryRobotStateStore, RobotStateStore

logger = logging.getLogger(__name__)

#: How long capability discovery may take before it is abandoned, in seconds.
DEFAULT_DISCOVERY_TIMEOUT = 10.0


class UnknownRobotError(LookupError):
    """An operation named a robot that is not registered."""


class RobotRuntime:
    """Everything the robot subsystem needs at runtime, in one place."""

    def __init__(
        self,
        *,
        events: EventBus | None = None,
        store: RobotStateStore | None = None,
        capabilities: RobotCapabilityRegistry | None = None,
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
    ) -> None:
        self._events = events or EventBus()
        self._store = store or InMemoryRobotStateStore()
        self._capabilities = capabilities or RobotCapabilityRegistry()
        self._registry = RobotRegistry(self._store, self._events, self._capabilities)
        self._channels: dict[str, ToolChannel] = {}
        self._discovery: dict[str, asyncio.Task[None]] = {}
        self._discovery_timeout = discovery_timeout
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def registry(self) -> RobotRegistry:
        return self._registry

    @property
    def states(self) -> RobotStateStore:
        return self._store

    @property
    def capabilities(self) -> RobotCapabilityRegistry:
        return self._capabilities

    def channel(self, robot_id: str) -> ToolChannel | None:
        return self._channels.get(robot_id)

    async def attach(
        self,
        device: DeviceInfo,
        channel: ToolChannel | None = None,
        *,
        discover: bool = True,
    ) -> RobotState:
        """Register a connected device and kick off capability discovery.

        Returns as soon as the robot is registered — discovery runs in the background.
        A reconnecting device rediscovers: firmware may have changed between sessions.
        """
        if self._closed:
            raise RuntimeError("robot runtime is closed")
        robot_id = device.robot_id
        state = await self._registry.register(device.identity(), device.connection())
        previous_channel = self._channels.get(robot_id)
        if channel is not None:
            self._channels[robot_id] = channel
            if previous_channel is not None and previous_channel is not channel:
                await _close_quietly(previous_channel, robot_id)
        if channel is not None and discover:
            # The channel confirms MCP support during discovery: at connection time the
            # device has not sent `hello` yet, so its feature block is not known here.
            self._start_discovery(robot_id)
        else:
            logger.info("robot %s: no tool channel, capabilities are feature flags only", robot_id)
            await self._registry.set_capabilities(robot_id, self._feature_capabilities(device))
        return state

    async def detach(
        self,
        robot_id: str,
        *,
        session_id: str | None = None,
        reason: DisconnectReason = DisconnectReason.CLIENT_CLOSED,
    ) -> bool:
        """Unregister a robot, cancel its discovery and close its tool channel.

        Idempotent, and a no-op when ``session_id`` belongs to a session that has already
        been replaced — double teardown is the normal flow in the inherited handler.
        """
        state = await self._registry.get(robot_id)
        if state is not None and session_id is not None and state.connection.session_id != session_id:
            return False
        removed = await self._registry.unregister(robot_id, session_id=session_id, reason=reason)
        if not removed:
            return False
        task = self._discovery.pop(robot_id, None)
        if task is not None and not task.done():
            task.cancel()
        channel = self._channels.pop(robot_id, None)
        if channel is not None:
            await _close_quietly(channel, robot_id)
        return True

    async def refresh_capabilities(self, robot_id: str) -> RobotCapabilities | None:
        """Run discovery now and wait for it. Returns ``None`` if there is no channel."""
        channel = self._channels.get(robot_id)
        if channel is None:
            return None
        capabilities = await asyncio.wait_for(channel.discover(), timeout=self._discovery_timeout)
        await self._registry.set_capabilities(robot_id, capabilities)
        return capabilities

    async def wait_for_discovery(self, robot_id: str) -> None:
        """Await the background discovery task, if one is running. Never raises."""
        task = self._discovery.get(robot_id)
        if task is None:
            return
        await asyncio.gather(task, return_exceptions=True)

    async def update_telemetry(self, robot_id: str, telemetry: RobotTelemetry) -> RobotState | None:
        return await self._registry.update_state(robot_id, telemetry)

    async def call_tool(
        self,
        robot_id: str,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        """Call a device tool, announcing start, completion and failure on the bus.

        ``timeout`` is passed through to the channel; the channel's own default is short
        on purpose, because an unacknowledged device command is a failure, not a wait.
        """
        channel = self._channels.get(robot_id)
        if channel is None:
            raise UnknownRobotError(f"robot {robot_id} has no tool channel")
        if not await self._capabilities.has_tool(robot_id, name):
            raise McpError(f"robot {robot_id} does not publish a tool named {name!r}")
        call_id = uuid4().hex
        payload = dict(arguments or {})
        started = time.monotonic()
        await self._events.publish(
            ToolCallStarted(robot_id=robot_id, call_id=call_id, tool_name=name, arguments=payload)
        )
        try:
            result = await channel.call_tool(name, payload, timeout=timeout)
        except Exception as exc:
            await self._events.publish(
                ToolCallFailed(
                    robot_id=robot_id,
                    call_id=call_id,
                    tool_name=name,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            )
            raise
        await self._events.publish(
            ToolCallCompleted(
                robot_id=robot_id,
                call_id=call_id,
                tool_name=name,
                result=result,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        )
        return result

    async def aclose(self) -> None:
        """Cancel discovery, close every channel and shut the bus down. Idempotent."""
        self._closed = True
        tasks = list(self._discovery.values())
        self._discovery.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for robot_id, channel in list(self._channels.items()):
            await _close_quietly(channel, robot_id)
        self._channels.clear()
        await self._events.drain()
        await self._events.aclose()

    def _start_discovery(self, robot_id: str) -> None:
        previous = self._discovery.pop(robot_id, None)
        if previous is not None and not previous.done():
            previous.cancel()
        self._discovery[robot_id] = asyncio.create_task(
            self._discover(robot_id), name=f"robot-discovery-{robot_id}"
        )

    @staticmethod
    def _feature_capabilities(device: DeviceInfo) -> RobotCapabilities:
        return RobotCapabilities(
            mcp=device.supports_mcp,
            features=frozenset(str(key) for key, value in device.features.items() if value),
        )

    async def _discover(self, robot_id: str) -> None:
        try:
            await self.refresh_capabilities(robot_id)
        except asyncio.CancelledError:
            raise
        except McpUnsupportedError:
            logger.info("robot %s does not speak MCP; no tool capabilities", robot_id)
            state = await self._registry.get(robot_id)
            if state is not None:
                await self._registry.set_capabilities(robot_id, RobotCapabilities(mcp=False))
        except TimeoutError:
            logger.warning("robot %s: capability discovery timed out", robot_id)
        except Exception as exc:
            logger.warning("robot %s: capability discovery failed: %s", robot_id, exc)


async def _close_quietly(channel: ToolChannel, robot_id: str) -> None:
    try:
        await channel.aclose()
    except Exception as exc:  # a channel that cannot be closed must not break teardown
        logger.warning("robot %s: closing the tool channel failed: %s", robot_id, exc)


_runtime: RobotRuntime | None = None


def get_runtime() -> RobotRuntime:
    """The process-wide runtime, created on first use.

    One holder, because the inherited server has no place to hang a control plane. The
    class itself has no module state, so tests build their own instead of using this.
    """
    global _runtime
    if _runtime is None or _runtime.closed:
        _runtime = RobotRuntime()
    return _runtime


def set_runtime(runtime: RobotRuntime | None) -> None:
    """Install (or clear, with ``None``) the process-wide runtime. For tests and app startup."""
    global _runtime
    _runtime = runtime
