"""The typed event vocabulary of the robot subsystem.

Events are frozen pydantic models with a common envelope (``event_id``, ``robot_id``,
``occurred_at``). Subscribers select by class, so a new event type is additive and a
subscriber of :class:`RobotEvent` sees everything.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from robot.state.models import (
    DisconnectReason,
    RobotBatteryState,
    RobotCapabilities,
    RobotConnection,
    RobotIdentity,
    RobotPose,
    RobotSensorState,
    RobotTelemetry,
    RobotTool,
    utcnow,
)


class RobotEvent(BaseModel):
    """Envelope every robot event carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = Field(default_factory=utcnow)
    robot_id: str

    @property
    def name(self) -> str:
        return type(self).__name__


class RobotConnected(RobotEvent):
    """A robot session was registered. ``reconnect`` is true for a returning device."""

    identity: RobotIdentity
    connection: RobotConnection
    reconnect: bool = False


class RobotDisconnected(RobotEvent):
    session_id: str
    reason: DisconnectReason = DisconnectReason.CLIENT_CLOSED


class TelemetryUpdated(RobotEvent):
    telemetry: RobotTelemetry
    changed: tuple[str, ...] = ()


class BatteryUpdated(RobotEvent):
    battery: RobotBatteryState


class PoseUpdated(RobotEvent):
    pose: RobotPose


class SensorUpdated(RobotEvent):
    sensors: RobotSensorState


class ToolDiscovered(RobotEvent):
    tool: RobotTool


class CapabilitiesRefreshed(RobotEvent):
    """Discovery finished (first connection or a refresh after a reconnect)."""

    capabilities: RobotCapabilities
    refreshed: bool = False


class ToolCallStarted(RobotEvent):
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallCompleted(RobotEvent):
    call_id: str
    tool_name: str
    result: str = ""
    duration_ms: float = 0.0


class ToolCallFailed(RobotEvent):
    call_id: str
    tool_name: str
    error: str
    duration_ms: float = 0.0


__all__ = [
    "BatteryUpdated",
    "CapabilitiesRefreshed",
    "PoseUpdated",
    "RobotConnected",
    "RobotDisconnected",
    "RobotEvent",
    "SensorUpdated",
    "TelemetryUpdated",
    "ToolCallCompleted",
    "ToolCallFailed",
    "ToolCallStarted",
    "ToolDiscovered",
]
