"""The action vocabulary: lifecycle, sources, resources and the record of one action.

This module holds the *nouns* the action system is written in, and nothing that moves.
It lives under ``robot/state/`` rather than ``robot/actions/`` for a layering reason
(docs/robot-architecture.md Sect. 7): ``robot/events/`` and ``robot/safety/`` both have to
name a status, a source and a resource, and neither is allowed to import
``robot/actions/``. Putting the vocabulary in the state layer — which both may import —
is what keeps the dependency graph acyclic without duplicating the enums.

Everything here is frozen, serializable and free of side effects, so a status transition
can be asserted on in a test with no event loop, no runtime and no device.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum, IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robot.state.models import utcnow


class ActionStatus(str, Enum):
    """Where one action is in its life.

    ``PENDING → STARTING → RUNNING → SUCCEEDED | FAILED | CANCELLED | TIMED_OUT``, plus
    ``REJECTED`` as the terminal state of an action that safety never admitted. Illegal
    transitions raise :class:`IllegalTransition` rather than being ignored: a lifecycle
    that silently absorbs a bad transition cannot be reasoned about after an incident.
    """

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """True while the action still holds (or is about to hold) its resources."""
        return self in ACTIVE_STATUSES


#: No transition leaves these.
TERMINAL_STATUSES: frozenset[ActionStatus] = frozenset(
    {
        ActionStatus.SUCCEEDED,
        ActionStatus.FAILED,
        ActionStatus.CANCELLED,
        ActionStatus.TIMED_OUT,
        ActionStatus.REJECTED,
    }
)

#: Statuses in which an action owns its resource claims.
ACTIVE_STATUSES: frozenset[ActionStatus] = frozenset(
    {ActionStatus.PENDING, ActionStatus.STARTING, ActionStatus.RUNNING}
)

#: The complete transition table. Anything not listed here raises.
#:
#: ``PENDING`` may be rejected because safety is evaluated twice — once on submission and
#: again immediately before dispatch, when the world may have changed under the queue.
ALLOWED_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.PENDING: frozenset(
        {
            ActionStatus.STARTING,
            ActionStatus.CANCELLED,
            ActionStatus.REJECTED,
            ActionStatus.TIMED_OUT,
            ActionStatus.FAILED,
        }
    ),
    ActionStatus.STARTING: frozenset(
        {
            ActionStatus.RUNNING,
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.CANCELLED,
            ActionStatus.TIMED_OUT,
        }
    ),
    ActionStatus.RUNNING: frozenset(
        {
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.CANCELLED,
            ActionStatus.TIMED_OUT,
        }
    ),
    ActionStatus.SUCCEEDED: frozenset(),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.CANCELLED: frozenset(),
    ActionStatus.TIMED_OUT: frozenset(),
    ActionStatus.REJECTED: frozenset(),
}


class IllegalTransition(RuntimeError):
    """An action was asked to move between two states the lifecycle does not connect."""

    def __init__(self, action_id: str, current: ActionStatus, requested: ActionStatus) -> None:
        super().__init__(f"action {action_id}: {current.value} -> {requested.value} is not a legal transition")
        self.action_id = action_id
        self.current = current
        self.requested = requested


def can_transition(current: ActionStatus, requested: ActionStatus) -> bool:
    return requested in ALLOWED_TRANSITIONS[current]


class ActionType(str, Enum):
    """The semantic vocabulary. One entry per thing a caller may ask the robot to do.

    Deliberately short and enumerable (docs/robot-architecture.md Sect. 3): a list this
    size can be reviewed, tested against the simulator and audited in a log.
    """

    MOVE = "move"
    TURN = "turn"
    STOP = "stop"
    LOOK_AT = "look_at"
    HEAD_ANGLE = "head_angle"
    LIFT = "lift"
    EXPRESSION = "expression"
    ANIMATION = "animation"
    CAPTURE_IMAGE = "capture_image"
    FOLLOW_TARGET = "follow_target"


#: Action types that command the drive base. These are the ones safety gates on sensors.
MOTION_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.MOVE, ActionType.TURN, ActionType.FOLLOW_TARGET}
)


class ActionSource(str, Enum):
    """Who asked. Recorded on every action so an incident log says where a motion came from.

    The source never widens what safety permits (docs/safety-model.md): a rejection is a
    rejection whether the LLM, a behaviour or an operator asked. It narrows in exactly one
    direction — :attr:`SAFETY` may command a stop while the emergency stop is engaged,
    because that is the layer doing the stopping.
    """

    USER = "user"
    LLM = "llm"
    BEHAVIOR = "behavior"
    SYSTEM = "system"
    SAFETY = "safety"


class ActionPriority(IntEnum):
    """Dispatch order. Higher wins, and a higher priority preempts a lower one.

    Not an open integer, so "priority 9999" is not a thing a caller can invent: the
    ceiling belongs to safety, and :attr:`EMERGENCY` is reachable only from
    :class:`ActionSource.SAFETY` paths.
    """

    IDLE = 0
    LOW = 10
    NORMAL = 50
    HIGH = 80
    EMERGENCY = 100


class Resource(str, Enum):
    """A physical subsystem exactly one action may command at a time.

    The set is fixed by docs/robot-architecture.md Sect. 2.8. ``DISPLAY`` is the face:
    expressions and animations both claim it, which is why an animation cannot run while
    a different expression is being held.
    """

    DRIVE = "drive"
    HEAD = "head"
    LIFT = "lift"
    DISPLAY = "display"
    AUDIO = "audio"
    CAMERA = "camera"


class RejectionReason(str, Enum):
    """Why safety said no. Typed, because "rejected" with a prose string is not auditable."""

    EMERGENCY_STOP_ENGAGED = "emergency_stop_engaged"
    DEVICE_DISCONNECTED = "device_disconnected"
    ROBOT_UNKNOWN = "robot_unknown"
    HEARTBEAT_EXPIRED = "heartbeat_expired"
    SENSOR_DATA_STALE = "sensor_data_stale"
    SENSOR_DATA_MISSING = "sensor_data_missing"
    CLIFF_HAZARD = "cliff_hazard"
    BUMP_HAZARD = "bump_hazard"
    ROBOT_LIFTED = "robot_lifted"
    OBSTACLE_TOO_CLOSE = "obstacle_too_close"
    DISTANCE_LIMIT_EXCEEDED = "distance_limit_exceeded"
    ANGLE_LIMIT_EXCEEDED = "angle_limit_exceeded"
    SPEED_LIMIT_EXCEEDED = "speed_limit_exceeded"
    DURATION_LIMIT_EXCEEDED = "duration_limit_exceeded"
    TTL_EXPIRED = "ttl_expired"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    UNSUPPORTED_ACTION = "unsupported_action"
    PARAMETER_OUT_OF_RANGE = "parameter_out_of_range"
    BATTERY_TOO_LOW = "battery_too_low"


class ActionError(BaseModel):
    """Why an action did not succeed. Carried on the terminal record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str = ""
    reason: RejectionReason | None = None

    @classmethod
    def rejected(cls, reason: RejectionReason, message: str = "") -> ActionError:
        return cls(code=reason.value, message=message or reason.value, reason=reason)


class ActionRecord(BaseModel):
    """An immutable snapshot of one action: the shape that is logged, published and queried.

    Every field the design requires is here and non-optional where it can be
    (docs/robot-actions.md): ``action_id``, ``robot_id``, ``created_at``, ``started_at``,
    ``finished_at``, ``timeout_s``, ``priority``, ``source``, ``status``, ``parameters``
    and exactly one of ``result`` / ``error``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str
    robot_id: str
    action_type: ActionType
    source: ActionSource
    priority: ActionPriority
    status: ActionStatus
    parameters: dict[str, Any] = Field(default_factory=dict)
    resources: tuple[Resource, ...] = ()
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    timeout_s: float = 0.0
    ttl_s: float | None = None
    device_action_id: str | None = None
    result: dict[str, Any] | None = None
    error: ActionError | None = None
    #: The trace this action belongs to. Set from the ambient context when the action is
    #: created and carried through dispatch and completion, which is what links a device
    #: response back to the utterance that caused it (``robot/correlation.py``).
    correlation_id: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def succeeded(self) -> bool:
        return self.status is ActionStatus.SUCCEEDED

    @property
    def rejection(self) -> RejectionReason | None:
        return self.error.reason if self.error is not None else None

    @property
    def duration_s(self) -> float | None:
        """Wall time from dispatch to terminal state, or ``None`` if it never started."""
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def age_s(self, now: datetime | None = None) -> float:
        """How long since the action was created. What the TTL check measures."""
        return ((now or utcnow()) - self.created_at).total_seconds()


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
    "IllegalTransition",
    "RejectionReason",
    "Resource",
    "can_transition",
]
