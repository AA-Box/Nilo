"""The seam between a live voice session and the robot subsystem.

This is the only robot module that knows about ``core.connection.ConnectionHandler``,
and it imports ``core/`` **lazily**, inside functions, so ``import robot`` still works in
a process with no config file and no loguru (docs/robot-architecture.md Sect. 7, R12).

Three entry points, all of which swallow their own failures: a robot problem must never
break a voice session.

    attach_connection(conn)       # after the headers are parsed
    detach_connection(conn)       # in handle_connection's finally
    handle_notification(conn, p)  # from the inherited MCP handler, for notifications/*

Capability discovery reuses the inherited device-MCP client rather than opening a second
handshake on the same channel: ``core`` already sends ``initialize``, follows
``tools/list`` pagination and de-sanitizes tool names, and a second client on one
WebSocket would collide with its request ids (robot-architecture R10).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from robot.devices.mcp import (
    DEFAULT_CALL_TIMEOUT,
    MalformedToolError,
    McpTimeoutError,
    McpUnsupportedError,
    supports_mcp,
    tool_from_mcp,
)
from robot.runtime import RobotRuntime, get_runtime
from robot.state.models import DeviceInfo, DisconnectReason, RobotCapabilities
from robot.telemetry import ingest as ingest_notification
from robot.telemetry import is_telemetry

logger = logging.getLogger(__name__)

#: The one attribute robot code adds to a ConnectionHandler. The inherited handler has no
#: fixed shape and collects attributes from several modules, so robot state is namespaced.
ROBOT_ATTR = "nilo_robot"

#: How long to wait for the device to finish the inherited MCP handshake, in seconds.
DEFAULT_READY_TIMEOUT = 15.0
_POLL_INTERVAL = 0.2


@dataclass(frozen=True, slots=True)
class RobotSession:
    """What robot code keeps on a connection: identifiers, nothing else."""

    robot_id: str
    device_id: str
    session_id: str


class ConnectionToolChannel:
    """A :class:`~robot.devices.tools.ToolChannel` over the inherited device-MCP client.

    ``discover`` waits for the handshake ``core`` performs after ``hello`` and reads the
    result; ``call_tool`` dispatches through ``call_mcp_tool`` with an explicit short
    timeout, because its own default of 30 s is two orders of magnitude too long for a
    device acknowledgement.
    """

    def __init__(
        self,
        conn: Any,
        *,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self._conn = conn
        self._ready_timeout = ready_timeout
        self._call_timeout = call_timeout

    async def discover(self) -> RobotCapabilities:
        client = await self._wait_until_ready()
        tools = []
        malformed = 0
        for definition in list(getattr(client, "tools", {}).values()):
            try:
                tools.append(tool_from_mcp(definition))
            except (MalformedToolError, ValueError) as exc:
                malformed += 1
                logger.warning("skipping malformed device tool definition: %s", exc)
        features = getattr(self._conn, "features", None) or {}
        return RobotCapabilities(
            mcp=True,
            features=frozenset(str(key) for key, value in features.items() if value),
            tools=tuple(tools),
            malformed_tools=malformed,
        )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

        client = getattr(self._conn, "mcp_client", None)
        if client is None:
            raise McpUnsupportedError("this session has no device MCP client")
        seconds = max(1, int(round(timeout if timeout is not None else self._call_timeout)))
        result = await call_mcp_tool(self._conn, client, name, json.dumps(dict(arguments or {})), seconds)
        return str(result)

    async def aclose(self) -> None:
        self._conn = None

    async def _wait_until_ready(self) -> Any:
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            conn = self._conn
            if conn is None:
                raise McpUnsupportedError("the session closed before MCP discovery started")
            client = getattr(conn, "mcp_client", None)
            features = getattr(conn, "features", None)
            if client is None and features is not None and not supports_mcp(features):
                raise McpUnsupportedError("the device did not advertise MCP support")
            if client is not None and await client.is_ready():
                return client
            await asyncio.sleep(_POLL_INTERVAL)
        raise McpTimeoutError(f"the device MCP handshake did not finish within {self._ready_timeout}s")


async def attach_connection(conn: Any, runtime: RobotRuntime | None = None) -> RobotSession | None:
    """Register a connected device as a robot. Returns ``None`` if it cannot be.

    Never raises: called from the inherited connection handler, where a robot failure
    must not take down a voice session.
    """
    try:
        headers = getattr(conn, "headers", None) or {}
        device_id = getattr(conn, "device_id", None) or headers.get("device-id")
        if not device_id:
            return None
        device = DeviceInfo(
            device_id=str(device_id),
            session_id=str(getattr(conn, "session_id", "") or ""),
            name=str(headers.get("device-name") or device_id),
            remote_address=getattr(conn, "client_ip", None),
            features=dict(getattr(conn, "features", None) or {}),
        )
        active = runtime or get_runtime()
        state = await active.attach(device, ConnectionToolChannel(conn))
        session = RobotSession(state.robot_id, device.device_id, device.session_id)
        setattr(conn, ROBOT_ATTR, session)
        return session
    except Exception as exc:
        logger.warning("robot attach failed for this session: %s", exc)
        return None


async def handle_notification(conn: Any, payload: Mapping[str, Any], runtime: RobotRuntime | None = None) -> bool:
    """Apply a device MCP notification to this session's robot. Returns whether it was ours.

    Called from the inherited device-MCP handler for every payload that carries a
    ``method`` — the branch that otherwise logs the method name and drops the frame. Only
    the methods in :data:`robot.telemetry.TELEMETRY_METHODS` are claimed; anything else
    returns ``False`` and the inherited logging still happens.

    Never raises, so the call site in ``core/`` needs no ``try``: an unregistered session,
    a malformed payload and a closed runtime are all ``False``, not exceptions.
    """
    try:
        method = payload.get("method")
        if not is_telemetry(method):
            return False
        session = getattr(conn, ROBOT_ATTR, None)
        if not isinstance(session, RobotSession):
            return False
        params = payload.get("params")
        active = runtime or get_runtime()
        patch = params if isinstance(params, Mapping) else None
        return await ingest_notification(active, session.robot_id, str(method), patch)
    except Exception as exc:
        logger.warning("robot notification handling failed: %s", exc)
        return False


async def detach_connection(
    conn: Any,
    runtime: RobotRuntime | None = None,
    *,
    reason: DisconnectReason = DisconnectReason.CLIENT_CLOSED,
) -> bool:
    """Unregister the robot bound to this session. Idempotent, and never raises."""
    session = getattr(conn, ROBOT_ATTR, None)
    if not isinstance(session, RobotSession):
        return False
    # Imported here rather than at module scope: robot/voice imports this module, so the
    # other direction would be a cycle.
    from robot.voice.seam import detach as detach_voice

    await detach_voice(conn)
    try:
        active = runtime or get_runtime()
        return await active.detach(session.robot_id, session_id=session.session_id, reason=reason)
    except Exception as exc:
        logger.warning("robot detach failed for %s: %s", session.robot_id, exc)
        return False
    finally:
        setattr(conn, ROBOT_ATTR, None)
