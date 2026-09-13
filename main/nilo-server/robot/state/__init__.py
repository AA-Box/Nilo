"""Robot world state: the domain models, the action vocabulary, and the store.

Import models from here; the split between ``models``, ``actions`` and ``store`` is an
implementation detail. Nothing in this package imports ``core/``, and nothing imports the
event bus — publishing is the caller's job, which keeps state readable from a plain test.

``actions`` holds the *vocabulary* of the action system — statuses, sources, resources,
the transition table and the frozen record — but none of its machinery. It lives here so
that ``robot/events`` and ``robot/safety`` can name a status without importing
``robot/actions``, which the layering forbids them (docs/robot-architecture.md Sect. 7).
"""

from robot.state.actions import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    MOTION_ACTIONS,
    TERMINAL_STATUSES,
    ActionError,
    ActionPriority,
    ActionRecord,
    ActionSource,
    ActionStatus,
    ActionType,
    IllegalTransition,
    RejectionReason,
    Resource,
    can_transition,
)
from robot.state.models import (
    ConnectionStatus,
    DeviceInfo,
    DisconnectReason,
    RobotActivity,
    RobotActivityState,
    RobotAudioState,
    RobotBatteryState,
    RobotCapabilities,
    RobotConnection,
    RobotExpressionState,
    RobotIdentity,
    RobotMotionState,
    RobotPose,
    RobotSensorState,
    RobotState,
    RobotTelemetry,
    RobotTool,
    RobotVisionState,
    StaleStateError,
    Timestamped,
    normalize_robot_id,
    utcnow,
)
from robot.state.store import InMemoryRobotStateStore, RobotStateStore

__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "MOTION_ACTIONS",
    "TERMINAL_STATUSES",
    "ActionError",
    "ActionPriority",
    "ActionRecord",
    "ActionSource",
    "ActionStatus",
    "ActionType",
    "ConnectionStatus",
    "DeviceInfo",
    "DisconnectReason",
    "IllegalTransition",
    "InMemoryRobotStateStore",
    "RobotActivity",
    "RobotActivityState",
    "RobotAudioState",
    "RobotBatteryState",
    "RobotCapabilities",
    "RobotConnection",
    "RobotExpressionState",
    "RobotIdentity",
    "RobotMotionState",
    "RejectionReason",
    "Resource",
    "RobotPose",
    "RobotSensorState",
    "RobotState",
    "RobotStateStore",
    "RobotTelemetry",
    "RobotTool",
    "RobotVisionState",
    "StaleStateError",
    "Timestamped",
    "can_transition",
    "normalize_robot_id",
    "utcnow",
]
