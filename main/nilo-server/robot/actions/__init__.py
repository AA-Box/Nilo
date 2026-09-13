"""Robot actions: the abstraction between anything that wants something and the hardware.

Nothing above this package speaks to a device. An LLM tool handler, a behaviour and a
management API all do the same thing — build a typed spec, submit it, and get an action
back — and the executor decides, in one place, whether it is safe, when it runs, and what
it is allowed to command.

    from robot.actions import MoveAction, RobotActionExecutor
    from robot.state.actions import ActionSource

    executor = RobotActionExecutor(runtime)
    action = await executor.submit(MoveAction(distance_mm=300), "nilo-sim-01", source=ActionSource.LLM)
    record = await action.wait()          # or executor.query(action.action_id), later

    handle = executor.robot("nilo-sim-01")
    await handle.move(distance_mm=300)    # the same thing, semantically

The four pieces, and why each is separate:

``model``      the ten action specs and the lifecycle object. No I/O, no queueing.
``queue``      dispatch order and the resource ledger. Synchronous; never awaits.
``registry``   every action by id, plus the device-id mapping completions resolve through.
``executor``   the only thing that calls a device, and the only thing that changes a status.

Layering (docs/robot-architecture.md Sect. 7): this package may import ``robot/state``,
``robot/safety``, ``robot/events`` and ``robot/devices``. It must never import
``robot/behavior`` or ``robot/personality`` — they sit above it and propose actions; they
do not execute them.
"""

from robot.actions.executor import SUPERVISE_INTERVAL_S, ActionRuntime, RobotActionExecutor
from robot.actions.model import (
    DEFAULT_TIMEOUT_S,
    DISPATCH_TIMEOUT_S,
    SPEC_TYPES,
    ActionSpec,
    AnimationAction,
    CaptureImageAction,
    ExpressionAction,
    FollowTargetAction,
    HeadAngleAction,
    LiftAction,
    LookAtAction,
    MoveAction,
    RobotAction,
    StopAction,
    TurnAction,
)
from robot.actions.queue import ResourceConflict, RobotActionQueue
from robot.actions.registry import RobotActionRegistry
from robot.actions.semantic import RobotHandle
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
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "DISPATCH_TIMEOUT_S",
    "SPEC_TYPES",
    "SUPERVISE_INTERVAL_S",
    "ActionError",
    "ActionPriority",
    "ActionRecord",
    "ActionRuntime",
    "ActionSource",
    "ActionSpec",
    "ActionStatus",
    "ActionType",
    "AnimationAction",
    "CaptureImageAction",
    "ExpressionAction",
    "FollowTargetAction",
    "HeadAngleAction",
    "IllegalTransition",
    "LiftAction",
    "LookAtAction",
    "MoveAction",
    "RejectionReason",
    "Resource",
    "ResourceConflict",
    "RobotAction",
    "RobotActionExecutor",
    "RobotActionQueue",
    "RobotActionRegistry",
    "RobotHandle",
    "StopAction",
    "TurnAction",
]
