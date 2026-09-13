"""Robot events: the typed vocabulary and the in-process bus that carries it."""

from robot.events.bus import DEFAULT_QUEUE_SIZE, EventBus, EventHandler, Subscription
from robot.events.types import (
    BatteryUpdated,
    CapabilitiesRefreshed,
    MotionCompleted,
    MotionFailed,
    PoseUpdated,
    RobotConnected,
    RobotDisconnected,
    RobotEvent,
    SensorUpdated,
    TelemetryUpdated,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallStarted,
    ToolDiscovered,
)

__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "BatteryUpdated",
    "CapabilitiesRefreshed",
    "EventBus",
    "EventHandler",
    "MotionCompleted",
    "MotionFailed",
    "PoseUpdated",
    "RobotConnected",
    "RobotDisconnected",
    "RobotEvent",
    "SensorUpdated",
    "Subscription",
    "TelemetryUpdated",
    "ToolCallCompleted",
    "ToolCallFailed",
    "ToolCallStarted",
    "ToolDiscovered",
]
