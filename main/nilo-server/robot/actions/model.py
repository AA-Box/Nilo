"""The ten semantic actions, and the lifecycle object that carries one through the system.

Two kinds of thing live here:

* **Action specs** — :class:`MoveAction`, :class:`TurnAction` and the eight others. Each is
  a frozen pydantic model of *what was asked for*, plus the mapping to the device tool that
  performs it. They hold no state and no timestamps, so the same spec can be submitted
  twice and produce two independent actions.
* **:class:`RobotAction`** — one submission of one spec: identity, timestamps, status,
  resource claims, result or error. This is the only mutable object in the action system,
  and the only thing that may change its own status.

The parameter vocabulary follows the firmware contract (docs/mcp.md, docs/robot-roadmap.md
Phase 4): **integers only**, the unit in the name (``distance_mm``, ``angle_deg``,
``speed_mmps``), no floats anywhere, and no name that refers to an actuator. A float
parameter is a latent bug — the device MCP type system carries booleans, integers and
strings, and nothing on the server would reject one.

Specs deliberately do **not** range-check their numbers. Bounds are policy, they are
configurable, and they belong to :mod:`robot.safety.policy`, which produces a typed
:class:`~robot.state.actions.RejectionReason` instead of an exception. One place decides
what is allowed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from typing import Any, ClassVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator

from robot.state.actions import (
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
from robot.state.models import utcnow

#: How long to wait for the device to *acknowledge* a dispatch, in seconds. Not how long
#: the motion may take: that is the action's own timeout. The inherited device-MCP default
#: is 30 s (``core/providers/tools/device_mcp/mcp_handler.py:call_mcp_tool``), which is two
#: orders of magnitude too long for an acknowledgement, and nothing else overrides it.
DISPATCH_TIMEOUT_S = 2.0

#: Fallback completion budget for an action whose duration cannot be estimated.
DEFAULT_TIMEOUT_S = 10.0

#: Slack added to an estimated duration before the watchdog calls the action late.
TIMEOUT_MARGIN_S = 3.0


class ActionSpec(BaseModel):
    """What a caller asked the robot to do. Frozen, stateless, unit-carrying.

    Subclasses set four class variables and implement :meth:`arguments`:

    ``action_type``       which member of :class:`~robot.state.actions.ActionType` this is
    ``resources``         the physical subsystems it claims for its whole lifetime
    ``device_tool_name``  the tool as the *device* publishes it, dots and all
    ``tool_name``         the same tool as the *server* keys it, after sanitization
    ``awaits_completion`` whether the device reports completion asynchronously

    Two names because there are two namespaces. Firmware publishes ``robot.motion.move``;
    the server replaces every character an LLM function name may not contain and keys
    everything on ``robot_motion_move``
    (``robot/devices/mcp.py:sanitize_tool_name``), calling back with the raw name it
    remembered. Capability lookups and dispatch therefore use :attr:`tool_name`;
    :attr:`device_tool_name` is what a firmware author reads.
    ``tests/robot/test_actions.py`` asserts the two stay in step, so the pair cannot drift.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_type: ClassVar[ActionType]
    resources: ClassVar[tuple[Resource, ...]] = ()
    device_tool_name: ClassVar[str]
    tool_name: ClassVar[str]
    #: False for a tool whose reply *is* the completion (a servo angle, a face change).
    #: True for one that returns an ``action_id`` and reports completion by notification.
    awaits_completion: ClassVar[bool] = False

    def arguments(self) -> dict[str, Any]:
        """The ``tools/call`` arguments. Integers, booleans and strings only."""
        raise NotImplementedError

    def estimated_duration_s(self) -> float | None:
        """How long the robot should need, if that is knowable from the parameters."""
        return None

    def suggested_timeout_s(self) -> float:
        """The completion budget the watchdog enforces when the caller does not set one."""
        estimate = self.estimated_duration_s()
        return DEFAULT_TIMEOUT_S if estimate is None else estimate + TIMEOUT_MARGIN_S

    def describe(self) -> str:
        arguments = ", ".join(f"{key}={value!r}" for key, value in self.arguments().items())
        return f"{self.action_type.value}({arguments})"


# -- motion ---------------------------------------------------------------------------------


class MoveAction(ActionSpec):
    """Drive straight by a bounded distance. Negative distances drive backwards."""

    action_type: ClassVar[ActionType] = ActionType.MOVE
    resources: ClassVar[tuple[Resource, ...]] = (Resource.DRIVE,)
    device_tool_name: ClassVar[str] = "robot.motion.move"
    tool_name: ClassVar[str] = "robot_motion_move"
    awaits_completion: ClassVar[bool] = True

    distance_mm: int
    speed_mmps: int = 200

    def arguments(self) -> dict[str, Any]:
        return {"distance_mm": self.distance_mm, "speed_mmps": self.speed_mmps}

    def estimated_duration_s(self) -> float | None:
        if self.speed_mmps <= 0:
            return None
        return abs(self.distance_mm) / self.speed_mmps


class TurnAction(ActionSpec):
    """Rotate in place by a bounded angle. Positive angles turn left (counter-clockwise)."""

    action_type: ClassVar[ActionType] = ActionType.TURN
    resources: ClassVar[tuple[Resource, ...]] = (Resource.DRIVE,)
    device_tool_name: ClassVar[str] = "robot.motion.turn"
    tool_name: ClassVar[str] = "robot_motion_turn"
    awaits_completion: ClassVar[bool] = True

    angle_deg: int
    speed_dps: int = 90

    def arguments(self) -> dict[str, Any]:
        return {"angle_deg": self.angle_deg, "speed_dps": self.speed_dps}

    def estimated_duration_s(self) -> float | None:
        if self.speed_dps <= 0:
            return None
        return abs(self.angle_deg) / self.speed_dps


class StopAction(ActionSpec):
    """Cancel the motion in flight.

    Stop is the one action that is never queued and never rejected for a hazard: a robot
    that will not stop because a sensor says it is unsafe to move is the wrong failure
    (docs/safety.md, "Emergency stop"). It still claims :attr:`Resource.DRIVE` so the
    ledger stays honest, but the executor dispatches it ahead of the queue.
    """

    action_type: ClassVar[ActionType] = ActionType.STOP
    resources: ClassVar[tuple[Resource, ...]] = (Resource.DRIVE,)
    device_tool_name: ClassVar[str] = "robot.motion.stop"
    tool_name: ClassVar[str] = "robot_motion_stop"

    reason: str = "stop requested"

    def arguments(self) -> dict[str, Any]:
        return {}

    def estimated_duration_s(self) -> float | None:
        return 0.0


class FollowTargetAction(ActionSpec):
    """Keep a tracked target framed, for a bounded time.

    Following is closed-loop and therefore firmware's job: the backend names the target
    and the deadline, the device does the tracking. It claims the drive, the head and the
    camera at once, which is why nothing else can look around while it runs.
    """

    action_type: ClassVar[ActionType] = ActionType.FOLLOW_TARGET
    resources: ClassVar[tuple[Resource, ...]] = (Resource.DRIVE, Resource.HEAD, Resource.CAMERA)
    device_tool_name: ClassVar[str] = "robot.follow.target"
    tool_name: ClassVar[str] = "robot_follow_target"
    awaits_completion: ClassVar[bool] = True

    target_id: str
    duration_ms: int = 5000
    stop_distance_mm: int = 600

    @field_validator("target_id")
    @classmethod
    def _target_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target_id must not be empty")
        return value

    def arguments(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "duration_ms": self.duration_ms,
            "stop_distance_mm": self.stop_distance_mm,
        }

    def estimated_duration_s(self) -> float | None:
        return self.duration_ms / 1000.0


# -- head, lift -------------------------------------------------------------------------------


class LookAtAction(ActionSpec):
    """Point the head at a spot in the camera frame, in percent of width and height.

    Percentages rather than pixels so a caller need not know the camera resolution, and
    integers rather than 0.0-1.0 floats because the firmware vocabulary has no float type.
    """

    action_type: ClassVar[ActionType] = ActionType.LOOK_AT
    resources: ClassVar[tuple[Resource, ...]] = (Resource.HEAD,)
    device_tool_name: ClassVar[str] = "robot.head.look_at"
    tool_name: ClassVar[str] = "robot_head_look_at"

    x_pct: int = 50
    y_pct: int = 50

    def arguments(self) -> dict[str, Any]:
        return {"x_pct": self.x_pct, "y_pct": self.y_pct}


class HeadAngleAction(ActionSpec):
    """Point the head at an absolute pitch and yaw. The device clamps to its stops."""

    action_type: ClassVar[ActionType] = ActionType.HEAD_ANGLE
    resources: ClassVar[tuple[Resource, ...]] = (Resource.HEAD,)
    device_tool_name: ClassVar[str] = "robot.head.set_angle"
    tool_name: ClassVar[str] = "robot_head_set_angle"

    pitch_deg: int = 0
    yaw_deg: int = 0

    def arguments(self) -> dict[str, Any]:
        return {"pitch_deg": self.pitch_deg, "yaw_deg": self.yaw_deg}


class LiftAction(ActionSpec):
    """Raise or lower the lift, as a percentage of its travel."""

    action_type: ClassVar[ActionType] = ActionType.LIFT
    resources: ClassVar[tuple[Resource, ...]] = (Resource.LIFT,)
    device_tool_name: ClassVar[str] = "robot.lift.set_position"
    tool_name: ClassVar[str] = "robot_lift_set_position"

    height_pct: int = 0

    def arguments(self) -> dict[str, Any]:
        return {"height_pct": self.height_pct}


# -- face -------------------------------------------------------------------------------------


class ExpressionAction(ActionSpec):
    """Show an emotion on the face. Claims the display, so it conflicts with an animation."""

    action_type: ClassVar[ActionType] = ActionType.EXPRESSION
    resources: ClassVar[tuple[Resource, ...]] = (Resource.DISPLAY,)
    device_tool_name: ClassVar[str] = "robot.expression.set"
    tool_name: ClassVar[str] = "robot_expression_set"

    emotion: str = "neutral"
    intensity_pct: int = 100

    @field_validator("emotion")
    @classmethod
    def _emotion_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("emotion must not be empty")
        return value

    def arguments(self) -> dict[str, Any]:
        return {"emotion": self.emotion, "intensity_pct": self.intensity_pct}


class AnimationAction(ActionSpec):
    """Play a named, pre-authored animation for a bounded time."""

    action_type: ClassVar[ActionType] = ActionType.ANIMATION
    resources: ClassVar[tuple[Resource, ...]] = (Resource.DISPLAY,)
    device_tool_name: ClassVar[str] = "robot.animation.play"
    tool_name: ClassVar[str] = "robot_animation_play"

    name: str
    duration_ms: int = 2000

    @field_validator("name")
    @classmethod
    def _name_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("animation name must not be empty")
        return value

    def arguments(self) -> dict[str, Any]:
        return {"name": self.name, "duration_ms": self.duration_ms}

    def estimated_duration_s(self) -> float | None:
        return self.duration_ms / 1000.0


# -- camera -----------------------------------------------------------------------------------


class CaptureImageAction(ActionSpec):
    """Capture one camera frame, optionally asking the vision endpoint a question about it."""

    action_type: ClassVar[ActionType] = ActionType.CAPTURE_IMAGE
    resources: ClassVar[tuple[Resource, ...]] = (Resource.CAMERA,)
    device_tool_name: ClassVar[str] = "robot.camera.capture"
    tool_name: ClassVar[str] = "robot_camera_capture"

    question: str | None = None

    def arguments(self) -> dict[str, Any]:
        return {"question": self.question} if self.question else {}

    def suggested_timeout_s(self) -> float:
        # A capture with a question goes out to the vision endpoint over HTTP.
        return 20.0 if self.question else DEFAULT_TIMEOUT_S


#: Every spec class, by the action type it implements. Used by the bridge and the tests
#: that enumerate the vocabulary; a new action that is not in here is not dispatchable.
SPEC_TYPES: dict[ActionType, type[ActionSpec]] = {
    ActionType.MOVE: MoveAction,
    ActionType.TURN: TurnAction,
    ActionType.STOP: StopAction,
    ActionType.LOOK_AT: LookAtAction,
    ActionType.HEAD_ANGLE: HeadAngleAction,
    ActionType.LIFT: LiftAction,
    ActionType.EXPRESSION: ExpressionAction,
    ActionType.ANIMATION: AnimationAction,
    ActionType.CAPTURE_IMAGE: CaptureImageAction,
    ActionType.FOLLOW_TARGET: FollowTargetAction,
}


class RobotAction:
    """One submission of one spec, from creation to a terminal state.

    Not a pydantic model: it has an :class:`asyncio.Event` and a status that changes, and
    the frozen, serializable view of it is :meth:`record`. Everything published on the
    event bus or returned to a caller is a record, never this object, so no subscriber can
    mutate an action it merely observed.
    """

    __slots__ = (
        "_done",
        "action_id",
        "created_at",
        "device_action_id",
        "error",
        "finished_at",
        "priority",
        "result",
        "robot_id",
        "source",
        "spec",
        "started_at",
        "status",
        "timeout_s",
        "ttl_s",
    )

    def __init__(
        self,
        spec: ActionSpec,
        robot_id: str,
        *,
        source: ActionSource = ActionSource.SYSTEM,
        priority: ActionPriority = ActionPriority.NORMAL,
        timeout_s: float | None = None,
        ttl_s: float | None = None,
        action_id: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.spec = spec
        self.robot_id = robot_id
        self.source = source
        self.priority = priority
        self.action_id = action_id or uuid4().hex
        self.created_at = created_at or utcnow()
        self.timeout_s = spec.suggested_timeout_s() if timeout_s is None else float(timeout_s)
        self.ttl_s = ttl_s
        self.status = ActionStatus.PENDING
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.result: dict[str, Any] | None = None
        self.error: ActionError | None = None
        self.device_action_id: str | None = None
        self._done = asyncio.Event()

    # -- identity -------------------------------------------------------------------------

    @property
    def action_type(self) -> ActionType:
        return self.spec.action_type

    @property
    def resources(self) -> tuple[Resource, ...]:
        return self.spec.resources

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def succeeded(self) -> bool:
        return self.status is ActionStatus.SUCCEEDED

    def conflicts_with(self, other: RobotAction) -> bool:
        """Whether these two actions would command the same subsystem at the same time."""
        return self.robot_id == other.robot_id and bool(set(self.resources) & set(other.resources))

    # -- lifecycle ------------------------------------------------------------------------

    def transition(
        self,
        status: ActionStatus,
        *,
        result: dict[str, Any] | None = None,
        error: ActionError | None = None,
        now: datetime | None = None,
    ) -> ActionRecord:
        """Move to ``status``, or raise :class:`IllegalTransition`.

        Timestamps are set here and nowhere else: ``started_at`` on the first move out of
        ``PENDING`` into a dispatching state, ``finished_at`` on any terminal state. A
        terminal action raises rather than absorbing a second completion — the executor
        checks :attr:`is_terminal` first, which is how a duplicate device notification is
        ignored deliberately instead of by accident.
        """
        if not can_transition(self.status, status):
            raise IllegalTransition(self.action_id, self.status, status)
        moment = now or utcnow()
        if status in (ActionStatus.STARTING, ActionStatus.RUNNING) and self.started_at is None:
            self.started_at = moment
        if status.is_terminal:
            self.finished_at = moment
        if result is not None:
            self.result = result
        if error is not None:
            self.error = error
        self.status = status
        if status.is_terminal:
            self._done.set()
        return self.record()

    def reject(self, reason: RejectionReason, message: str = "", *, now: datetime | None = None) -> ActionRecord:
        """Terminal ``REJECTED`` with a typed reason. Never clamps, never half-executes."""
        return self.transition(ActionStatus.REJECTED, error=ActionError.rejected(reason, message), now=now)

    def fail(self, code: str, message: str = "", *, now: datetime | None = None) -> ActionRecord:
        return self.transition(ActionStatus.FAILED, error=ActionError(code=code, message=message), now=now)

    # -- views ----------------------------------------------------------------------------

    def record(self) -> ActionRecord:
        """The frozen snapshot. Everything that leaves the action system is one of these.

        ``parameters`` is the whole request (``spec.model_dump()``), not the narrower set of
        arguments that goes on the wire: a ``StopAction``'s ``reason`` sends nothing to the
        device but is the most useful field in the record afterwards.
        """
        return ActionRecord(
            action_id=self.action_id,
            robot_id=self.robot_id,
            action_type=self.action_type,
            source=self.source,
            priority=self.priority,
            status=self.status,
            parameters=self.spec.model_dump(),
            resources=self.resources,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            timeout_s=self.timeout_s,
            ttl_s=self.ttl_s,
            device_action_id=self.device_action_id,
            result=self.result,
            error=self.error,
        )

    async def wait(self, timeout: float | None = None) -> ActionRecord:
        """Block until the action is terminal. Returns its record.

        This is what ``await robot.move(...)`` awaits. It is **not** what an LLM tool
        handler awaits — those return the moment the action is accepted
        (docs/robot-architecture.md Sect. 5).
        """
        if timeout is None:
            await self._done.wait()
        else:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        return self.record()

    def __repr__(self) -> str:
        return (
            f"<RobotAction {self.action_id[:8]} {self.spec.describe()} "
            f"robot={self.robot_id} {self.status.value} p={int(self.priority)}>"
        )


def sort_key(action: RobotAction) -> tuple[int, float]:
    """Dispatch order: highest priority first, then oldest first.

    A plain key rather than a heap entry so the queue can re-sort after a priority change
    and stay readable; the queue is per robot and short.
    """
    return (-int(action.priority), action.created_at.timestamp())


def resources_of(actions: Iterable[RobotAction]) -> set[Resource]:
    claimed: set[Resource] = set()
    for action in actions:
        claimed.update(action.resources)
    return claimed


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "DISPATCH_TIMEOUT_S",
    "SPEC_TYPES",
    "TIMEOUT_MARGIN_S",
    "ActionSpec",
    "AnimationAction",
    "CaptureImageAction",
    "ExpressionAction",
    "FollowTargetAction",
    "HeadAngleAction",
    "LiftAction",
    "LookAtAction",
    "MoveAction",
    "RobotAction",
    "StopAction",
    "TurnAction",
    "resources_of",
    "sort_key",
]
