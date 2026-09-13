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

from robot.state.actions import ActionRecord
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


class MotionCompleted(RobotEvent):
    """A motion the robot accepted finished on its own. Reported by the device, not inferred."""

    action_id: str
    kind: str = "move"
    pose: RobotPose | None = None


class MotionFailed(RobotEvent):
    """A motion ended without finishing. ``reason`` is the device's word for why."""

    action_id: str
    kind: str = "move"
    reason: str = "unknown"
    detail: str = ""


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


class ActionSubmitted(RobotEvent):
    """An action was created and handed to the executor. Published even if it is rejected
    in the same breath, so the audit trail shows what was *asked for*, not only what ran."""

    action: ActionRecord


class ActionStarted(RobotEvent):
    """An action passed safety, took its resource claims, and is being dispatched."""

    action: ActionRecord


class ActionFinished(RobotEvent):
    """An action reached a terminal state — any of them.

    One event for every ending, because a subscriber that cares about "it is over" should
    not have to subscribe to five types to learn it. Which ending it was is
    ``event.action.status``, and why is ``event.action.error``.
    """

    action: ActionRecord

    @property
    def succeeded(self) -> bool:
        return self.action.succeeded


class EmergencyStopChanged(RobotEvent):
    """The emergency-stop latch was engaged or cleared for one robot."""

    engaged: bool
    reason: str = ""
    engaged_by: str = "system"


__all__ = [
    "ActionFinished",
    "ActionStarted",
    "ActionSubmitted",
    "BatteryUpdated",
    "CapabilitiesRefreshed",
    "EmergencyStopChanged",
    "MotionCompleted",
    "MotionFailed",
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
