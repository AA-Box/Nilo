"""The fourteen tools the model is offered, and the one place that runs them.

    toolkit = RobotToolkit(runtime, "nilo-sim-01")
    functions = toolkit.function_descriptions()          # OpenAI function-calling schemas
    outcome = await toolkit.call("robot_move", {"distance_mm": 250}, origin=TurnOrigin.USER)

Every tool here is **semantic**. There is no motor, no servo, no PWM duty cycle and no
wheel speed in any schema, because the vocabulary *is* the safety boundary
(docs/robot-architecture.md Sect. 3). A tool that could set a wheel speed would make every
layer below it decorative.

Four properties are load-bearing:

* **Nothing calls a device from here.** Motion, head, face and camera tools all go through
  :class:`~robot.actions.semantic.RobotHandle`, which is the executor, which is the only
  thing in the process that speaks to hardware. Memory tools go through
  :class:`~robot.memory.service.RobotMemory`. There is no third path.
* **Arguments are validated before anything is submitted.** Each tool has a frozen
  pydantic model with ``extra="forbid"`` and integer bounds taken from the *live* safety
  limits, so the schema the model reads and the ceiling the policy enforces are the same
  number. A validation failure is a returned error, never an exception into the chat loop.
* **Motion never waits.** Handlers pass ``wait=False``: the inherited chat loop awaits tool
  futures sequentially on a five-worker pool with no cancellation
  (``core/connection.py``), so a handler that waited for a robot to finish driving would
  pin a worker for the whole ``tool_call_timeout``. The model is told the action was
  accepted; completion arrives as an event.
* **A refusal is a result.** Safety rejections, permission refusals and unknown robots all
  come back as a :class:`ToolOutcome` the model can read and explain. Nothing here raises
  into the caller.

Integers only, with the unit in the name (``distance_mm``, ``angle_deg``, ``speed_mmps``).
The device MCP type system carries booleans, integers and strings and nothing on the server
would reject a float, so a float parameter is a latent bug rather than a style choice
(docs/mcp.md).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from robot.agent.permissions import PolicyDecision, ToolPermission, ToolPolicy, TurnOrigin
from robot.animation.library import EXPRESSIONS
from robot.behavior.base import AutonomyMode
from robot.state.actions import ActionRecord, ActionSource, ActionStatus, ActionType
from robot.state.world import EntityType

logger = logging.getLogger(__name__)

#: How long a single tool handler may run before it is abandoned. Generous for a capture,
#: ungenerous for anything that should have returned immediately.
DEFAULT_TOOL_TIMEOUT_S = 8.0

#: What ``robot_follow_person`` accepts instead of a person id, meaning "whoever is there".
NEAREST = "nearest"


class ToolError(ValueError):
    """A tool call that cannot be attempted: bad arguments, or an unknown tool."""


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What running a tool produced. Serializable, and always safe to hand to a model."""

    tool: str
    ok: bool
    result: dict[str, Any] | None = None
    error: str = ""
    #: Set when the refusal came from the permission policy or the safety layer rather
    #: than from the tool failing. The model is told *why*, so it can say so out loud.
    refused_by: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.refused_by)

    def as_text(self) -> str:
        """The string form handed back to the model."""
        if self.ok:
            return json.dumps(self.result or {"ok": True}, ensure_ascii=False, default=str)
        return json.dumps({"error": self.error, "refused_by": self.refused_by or None}, ensure_ascii=False)

    @classmethod
    def success(cls, tool: str, **result: Any) -> ToolOutcome:
        return cls(tool, True, result)

    @classmethod
    def failure(cls, tool: str, error: str, *, refused_by: str = "") -> ToolOutcome:
        return cls(tool, False, None, error, refused_by)


# -- argument models -------------------------------------------------------------------------


class ToolArguments(BaseModel):
    """Base for every tool's arguments. Frozen, and unknown keys are an error.

    ``extra="forbid"`` is the important half: a model that invents ``speed`` next to
    ``speed_mmps`` gets a clear error instead of a silently ignored parameter and a robot
    that moves at the default speed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Fields whose integer bounds are taken from the live safety limits when the schema
    #: is rendered: ``{field name: limits attribute}``. The schema a model reads and the
    #: ceiling the policy enforces then cannot drift.
    limit_bounds: ClassVar[dict[str, str]] = {}


class MoveArguments(ToolArguments):
    limit_bounds: ClassVar[dict[str, str]] = {"distance_mm": "max_distance_mm", "speed_mmps": "max_speed_mmps"}

    distance_mm: int = Field(description="How far to drive, in millimetres. Negative drives backwards.")
    speed_mmps: int = Field(default=200, gt=0, description="Drive speed in millimetres per second.")


class TurnArguments(ToolArguments):
    limit_bounds: ClassVar[dict[str, str]] = {"angle_deg": "max_angle_deg", "speed_dps": "max_turn_speed_dps"}

    angle_deg: int = Field(description="How far to turn in place, in degrees. Positive turns left.")
    speed_dps: int = Field(default=90, gt=0, description="Turn speed in degrees per second.")


class StopArguments(ToolArguments):
    reason: str = Field(default="stop requested", max_length=200, description="Why the robot is stopping.")


class LookAtArguments(ToolArguments):
    x_pct: int = Field(default=50, ge=0, le=100, description="Horizontal target, percent of the camera frame width.")
    y_pct: int = Field(default=50, ge=0, le=100, description="Vertical target, percent of the camera frame height.")


class FollowPersonArguments(ToolArguments):
    limit_bounds: ClassVar[dict[str, str]] = {"duration_ms": "max_follow_duration_ms"}

    person_id: str = Field(
        default=NEAREST,
        max_length=120,
        description=f"Which person to follow. Use {NEAREST!r} for whoever is closest.",
    )
    duration_ms: int = Field(default=5000, gt=0, description="How long to follow, in milliseconds.")
    stop_distance_mm: int = Field(default=600, gt=0, le=3000, description="How close to get before stopping.")


class StopFollowingArguments(ToolArguments):
    pass


class PlayAnimationArguments(ToolArguments):
    name: str = Field(max_length=80, description="Which pre-authored animation to play.")


class SetExpressionArguments(ToolArguments):
    emotion: str = Field(description="Which face to show.")
    intensity_pct: int = Field(default=100, ge=0, le=100, description="How strongly to show it.")


class CaptureImageArguments(ToolArguments):
    pass


class InspectObjectArguments(ToolArguments):
    question: str = Field(
        max_length=300,
        description="What to ask about what the camera can see, e.g. 'what colour is the mug?'.",
    )


class GetBatteryArguments(ToolArguments):
    pass


class GetStateArguments(ToolArguments):
    pass


class RememberArguments(ToolArguments):
    fact: str = Field(max_length=500, description="One thing worth remembering, in a single sentence.")
    person_id: str = Field(
        default="", max_length=120, description="Who this is about, when it is about somebody."
    )
    importance: int = Field(
        default=50, ge=0, le=100, description="How much this matters, 0 trivial to 100 critical."
    )


class RecallArguments(ToolArguments):
    query: str = Field(default="", max_length=300, description="What to look for in memory.")
    person_id: str = Field(default="", max_length=120, description="Restrict the search to one person.")
    limit: int = Field(default=5, ge=1, le=20, description="How many memories to return.")


# -- the catalogue ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RobotToolSpec:
    """One tool: how it is named, what it may do, and what it accepts."""

    #: The name the model calls. Flat and underscore-separated, because the tool namespace
    #: is flat and an OpenAI function name may not contain a dot.
    name: str
    #: The same tool in the dotted form the design note and the docs use.
    qualified_name: str
    description: str
    permission: ToolPermission
    arguments: type[ToolArguments]

    def schema(self, limits: Any = None, *, choices: Mapping[str, list[str]] | None = None) -> dict[str, Any]:
        """The OpenAI function-calling description, with live bounds folded in."""
        model_schema = self.arguments.model_json_schema()
        properties = {
            key: _clean_property(value) for key, value in (model_schema.get("properties") or {}).items()
        }
        if limits is not None:
            for field_name, limit_name in self.arguments.limit_bounds.items():
                ceiling = getattr(limits, limit_name, None)
                if isinstance(ceiling, int) and field_name in properties:
                    properties[field_name]["maximum"] = ceiling
                    properties[field_name]["minimum"] = -ceiling if _is_signed(field_name) else 1
        for field_name, values in (choices or {}).items():
            if field_name in properties:
                properties[field_name]["enum"] = list(values)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(model_schema.get("required") or []),
                },
            },
        }


#: Parameters that are meaningful in both directions: the schema's minimum is the negated
#: ceiling rather than one. Backwards is a direction, not a smaller number.
_SIGNED_FIELDS = frozenset({"distance_mm", "angle_deg"})


def _is_signed(field_name: str) -> bool:
    return field_name in _SIGNED_FIELDS


def _clean_property(schema: Any) -> dict[str, Any]:
    """Strip the pydantic bookkeeping a function-calling schema has no use for."""
    if not isinstance(schema, dict):
        return {"type": "string"}
    keep = ("type", "description", "default", "minimum", "maximum", "maxLength", "enum")
    return {key: value for key, value in schema.items() if key in keep}


TOOL_SPECS: tuple[RobotToolSpec, ...] = (
    RobotToolSpec(
        "robot_move",
        "robot.move",
        "Drive the robot straight forward or backward by a distance. Use a negative "
        "distance to back away. Returns as soon as the robot accepts the command.",
        ToolPermission.MOTION,
        MoveArguments,
    ),
    RobotToolSpec(
        "robot_turn",
        "robot.turn",
        "Turn the robot in place. Positive angles turn left, negative angles turn right.",
        ToolPermission.MOTION,
        TurnArguments,
    ),
    RobotToolSpec(
        "robot_stop",
        "robot.stop",
        "Stop whatever the robot is doing right now. Always available.",
        ToolPermission.MOTION,
        StopArguments,
    ),
    RobotToolSpec(
        "robot_look_at",
        "robot.look_at",
        "Point the robot's head at a spot in its camera view, given in percent of the "
        "frame. 50/50 is straight ahead.",
        ToolPermission.MOTION,
        LookAtArguments,
    ),
    RobotToolSpec(
        "robot_follow_person",
        "robot.follow_person",
        "Keep a person framed and follow them for a short, bounded time.",
        ToolPermission.MOTION,
        FollowPersonArguments,
    ),
    RobotToolSpec(
        "robot_stop_following",
        "robot.stop_following",
        "Stop following whoever the robot is following.",
        ToolPermission.MOTION,
        StopFollowingArguments,
    ),
    RobotToolSpec(
        "robot_play_animation",
        "robot.play_animation",
        "Play one of the robot's pre-authored animations.",
        ToolPermission.EXPRESSIVE,
        PlayAnimationArguments,
    ),
    RobotToolSpec(
        "robot_set_expression",
        "robot.set_expression",
        "Show an expression on the robot's face.",
        ToolPermission.EXPRESSIVE,
        SetExpressionArguments,
    ),
    RobotToolSpec(
        "robot_capture_image",
        "robot.capture_image",
        "Take one picture with the robot's camera and describe what is in it.",
        ToolPermission.PRIVILEGED,
        CaptureImageArguments,
    ),
    RobotToolSpec(
        "robot_inspect_object",
        "robot.inspect_object",
        "Take one picture and ask a specific question about what the camera can see.",
        ToolPermission.PRIVILEGED,
        InspectObjectArguments,
    ),
    RobotToolSpec(
        "robot_get_battery",
        "robot.get_battery",
        "Read the robot's battery percentage and whether it is charging.",
        ToolPermission.READ_ONLY,
        GetBatteryArguments,
    ),
    RobotToolSpec(
        "robot_get_state",
        "robot.get_state",
        "Read what the robot is doing right now: activity, motion, battery and what it can see.",
        ToolPermission.READ_ONLY,
        GetStateArguments,
    ),
    RobotToolSpec(
        "robot_remember",
        "robot.remember",
        "Write one thing into the robot's long-term memory. Use this when somebody tells "
        "the robot something worth keeping, not for small talk.",
        ToolPermission.PRIVILEGED,
        RememberArguments,
    ),
    RobotToolSpec(
        "robot_recall",
        "robot.recall",
        "Look something up in the robot's long-term memory.",
        ToolPermission.READ_ONLY,
        RecallArguments,
    ),
)

TOOLS_BY_NAME: dict[str, RobotToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

#: Every tool name, in catalogue order. Handy for a test and for the management API.
TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in TOOL_SPECS)


# -- the toolkit --------------------------------------------------------------------------------


class RobotToolkit:
    """The tool surface for one robot: schemas out, validated calls in.

    Holds no conversation state. One is built per agent, and a test builds one over a fake
    runtime with no device behind it at all.
    """

    def __init__(
        self,
        runtime: Any,
        robot_id: str,
        *,
        policy: ToolPolicy | None = None,
        memory: Any = None,
        animations: Any = None,
        timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
        source: ActionSource = ActionSource.LLM,
    ) -> None:
        self.runtime = runtime
        self.robot_id = robot_id
        self.policy = policy or ToolPolicy()
        self.memory = memory
        self.animations = animations
        self.timeout_s = timeout_s
        self.source = source

    # -- description ---------------------------------------------------------------------------

    @property
    def limits(self) -> Any:
        """The live safety limits, or ``None`` when the runtime has no executor.

        Read through the runtime rather than imported: ``robot/agent`` must not import
        ``robot/safety``, so that no layer above the policy can reach the policy
        (docs/robot-architecture.md Sect. 7).
        """
        actions = getattr(self.runtime, "actions", None)
        return getattr(actions, "limits", None)

    def animation_names(self) -> list[str]:
        library = getattr(self.animations, "library", None)
        names = getattr(library, "names", None)
        return sorted(names()) if callable(names) else []

    def specs(self) -> tuple[RobotToolSpec, ...]:
        return TOOL_SPECS

    def function_descriptions(self) -> list[dict[str, Any]]:
        """Every tool as an OpenAI function-calling description, bounds and enums included."""
        limits = self.limits
        animations = self.animation_names()
        descriptions = []
        for spec in TOOL_SPECS:
            choices: dict[str, list[str]] = {}
            if spec.name == "robot_set_expression":
                choices["emotion"] = list(EXPRESSIONS)
            if spec.name == "robot_play_animation" and animations:
                choices["name"] = animations
            descriptions.append(spec.schema(limits, choices=choices))
        return descriptions

    # -- calling ---------------------------------------------------------------------------------

    def validate(self, name: str, arguments: Mapping[str, Any] | None) -> ToolArguments:
        """Parse and bound-check one call's arguments. Raises :class:`ToolError`."""
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            raise ToolError(f"no robot tool named {name!r}")
        try:
            parsed = spec.arguments.model_validate(dict(arguments or {}))
        except ValidationError as exc:
            raise ToolError(_readable(exc)) from None
        self._check_limits(spec, parsed)
        self._check_choices(spec, parsed)
        return parsed

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        origin: TurnOrigin = TurnOrigin.USER,
        mode: AutonomyMode | None = None,
        person_id: str | None = None,
    ) -> ToolOutcome:
        """Validate, authorize and run one tool. Never raises."""
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            return ToolOutcome.failure(name, f"no robot tool named {name!r}")
        decision = self.authorize(spec, origin=origin, mode=mode)
        if not decision.allowed:
            logger.info("robot %s: refused %s — %s", self.robot_id, name, decision.reason)
            return ToolOutcome.failure(name, decision.reason, refused_by="permission_policy")
        try:
            parsed = self.validate(name, arguments)
        except ToolError as exc:
            return ToolOutcome.failure(name, str(exc), refused_by="argument_validation")
        handler = _HANDLERS[name]
        try:
            return await asyncio.wait_for(handler(self, parsed, person_id), timeout=self.timeout_s)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("robot %s: tool %s did not return within %.1fs", self.robot_id, name, self.timeout_s)
            return ToolOutcome.failure(name, f"{name} did not finish within {self.timeout_s:.0f}s", refused_by="timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("robot %s: tool %s failed", self.robot_id, name)
            return ToolOutcome.failure(name, f"{type(exc).__name__}: {exc}")

    def authorize(
        self, spec: RobotToolSpec, *, origin: TurnOrigin, mode: AutonomyMode | None = None
    ) -> PolicyDecision:
        """Whether the policy permits this tool on this turn."""
        active = mode if mode is not None else getattr(self.runtime, "autonomy", AutonomyMode.NORMAL)
        return self.policy.decide(spec.name, spec.permission, origin=origin, mode=active)

    def available(self, origin: TurnOrigin, mode: AutonomyMode | None = None) -> tuple[str, ...]:
        """The tools that would be permitted on a turn with this origin. For the context."""
        return tuple(
            spec.name for spec in TOOL_SPECS if self.authorize(spec, origin=origin, mode=mode).allowed
        )

    # -- internals ---------------------------------------------------------------------------------

    def _check_limits(self, spec: RobotToolSpec, parsed: ToolArguments) -> None:
        """Reject a value the safety policy would reject, here, where the model can read why.

        Duplicating the ceiling is deliberate: the policy still evaluates every action, and
        this check only makes the refusal legible to the model one step earlier. It can
        never *permit* something the policy would refuse.
        """
        limits = self.limits
        if limits is None:
            return
        for field_name, limit_name in spec.arguments.limit_bounds.items():
            ceiling = getattr(limits, limit_name, None)
            value = getattr(parsed, field_name, None)
            if not isinstance(ceiling, int) or not isinstance(value, int):
                continue
            if abs(value) > ceiling:
                raise ToolError(
                    f"{field_name}={value} is outside the configured limit of {ceiling} "
                    f"({'±' if _is_signed(field_name) else 'max '}{ceiling})"
                )

    @staticmethod
    def _check_choices(spec: RobotToolSpec, parsed: ToolArguments) -> None:
        if spec.name == "robot_set_expression":
            emotion = getattr(parsed, "emotion", "")
            if emotion not in EXPRESSIONS:
                raise ToolError(f"unknown expression {emotion!r}; choose one of {', '.join(EXPRESSIONS)}")

    def _handle(self) -> Any:
        """The semantic handle for this robot, attributed to the LLM."""
        return self.runtime.actions.robot(self.robot_id).as_source(self.source)


def _readable(error: ValidationError) -> str:
    """A pydantic error the model can act on, rather than a stack of dictionaries."""
    parts = []
    for problem in error.errors():
        location = ".".join(str(item) for item in problem.get("loc", ())) or "arguments"
        parts.append(f"{location}: {problem.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid arguments"


def _record_result(record: ActionRecord | None, tool: str, **extra: Any) -> ToolOutcome:
    """Turn an action record into an outcome the model can read.

    A safety rejection is a *result*, not a failure of this layer: the model is told which
    typed reason refused it so it can say "I cannot, there is a drop in front of me"
    instead of inventing an explanation.
    """
    if record is None:
        return ToolOutcome.failure(tool, "the robot did not accept the command")
    if record.status is ActionStatus.REJECTED:
        reason = record.rejection.value if record.rejection else "refused"
        message = record.error.message if record.error else reason
        return ToolOutcome.failure(tool, message, refused_by=f"safety:{reason}")
    if record.status in (ActionStatus.FAILED, ActionStatus.TIMED_OUT):
        message = record.error.message if record.error else record.status.value
        return ToolOutcome.failure(tool, message)
    return ToolOutcome.success(
        tool, action_id=record.action_id, status=record.status.value, **extra
    )


# -- handlers -------------------------------------------------------------------------------------
#
# One per tool. Each takes the toolkit, the already-validated arguments and the person the
# turn is about, and returns an outcome. None of them touches a device.


async def _move(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    record = await kit._handle().move(args.distance_mm, args.speed_mmps, wait=False)
    return _record_result(record, "robot_move", distance_mm=args.distance_mm)


async def _turn(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    record = await kit._handle().turn(args.angle_deg, args.speed_dps, wait=False)
    return _record_result(record, "robot_turn", angle_deg=args.angle_deg)


async def _stop(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    record = await kit._handle().stop(args.reason, wait=False)
    return _record_result(record, "robot_stop")


async def _look_at(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    record = await kit._handle().look_at(args.x_pct, args.y_pct, wait=False)
    return _record_result(record, "robot_look_at")


async def _follow_person(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    target = args.person_id
    if not target or target == NEAREST:
        resolved = _nearest_person(kit)
        if resolved is None:
            return ToolOutcome.failure("robot_follow_person", "the robot cannot see anybody to follow")
        target = resolved
    record = await kit._handle().follow(
        target, duration_ms=args.duration_ms, stop_distance_mm=args.stop_distance_mm, wait=False
    )
    return _record_result(record, "robot_follow_person", person_id=target)


async def _stop_following(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    executor = kit.runtime.actions
    running = [
        record
        for record in executor.running(kit.robot_id)
        if record.action_type is ActionType.FOLLOW_TARGET
    ]
    for record in running:
        await executor.cancel(record.action_id, reason="the model asked the robot to stop following")
    return ToolOutcome.success("robot_stop_following", cancelled=len(running))


async def _play_animation(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    engine = kit.animations
    if engine is not None:
        playback = await engine.play(args.name)
        if playback is None:
            return ToolOutcome.failure(
                "robot_play_animation",
                f"{args.name} did not play: it is unknown, or something else is using the face",
                refused_by="animation_engine",
            )
        return ToolOutcome.success("robot_play_animation", animation=args.name)
    record = await kit._handle().play_animation(args.name, wait=False)
    return _record_result(record, "robot_play_animation", animation=args.name)


async def _set_expression(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    record = await kit._handle().set_expression(args.emotion, args.intensity_pct, wait=False)
    return _record_result(record, "robot_set_expression", emotion=args.emotion)


async def _capture_image(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    record = await kit._handle().capture_image(None, wait=True)
    return _record_result(record, "robot_capture_image", seen=_seen(record))


async def _inspect_object(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    record = await kit._handle().capture_image(args.question, wait=True)
    return _record_result(record, "robot_inspect_object", answer=_seen(record))


async def _get_battery(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    state = await kit.runtime.get_state(kit.robot_id)
    battery = getattr(getattr(state, "telemetry", None), "battery", None)
    if battery is None:
        return ToolOutcome.failure("robot_get_battery", "the robot has not reported its battery yet")
    return ToolOutcome.success(
        "robot_get_battery", percent=battery.percent, charging=battery.charging, low=battery.is_low
    )


async def _get_state(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    state = await kit.runtime.get_state(kit.robot_id)
    if state is None:
        return ToolOutcome.failure("robot_get_state", f"robot {kit.robot_id} is not connected")
    telemetry = state.telemetry
    world = kit.runtime.world.snapshot(kit.robot_id)
    activity = telemetry.activity.activity.value if telemetry.activity else "unknown"
    return ToolOutcome.success(
        "robot_get_state",
        connected=state.is_connected,
        activity=activity,
        moving=bool(telemetry.motion.moving) if telemetry.motion else False,
        battery_percent=telemetry.battery.percent if telemetry.battery else None,
        people_visible=len(world.people),
        objects_visible=len(world.objects),
    )


async def _remember(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    memory = kit.memory
    if memory is None:
        return ToolOutcome.failure("robot_remember", "this robot has no long-term memory configured")
    subject = args.person_id or person_id or None
    episode = await memory.remember(
        args.fact, person_id=subject, importance=args.importance / 100.0, learned_from="llm"
    )
    return ToolOutcome.success("robot_remember", memory_id=episode.id, person_id=subject)


async def _recall(kit: RobotToolkit, args: Any, person_id: str | None) -> ToolOutcome:
    memory = kit.memory
    if memory is None:
        return ToolOutcome.failure("robot_recall", "this robot has no long-term memory configured")
    subject = args.person_id or person_id or None
    found = await memory.recall(args.query, person_id=subject, limit=args.limit)
    return ToolOutcome.success(
        "robot_recall",
        memories=[{"text": item.text, "kind": item.kind.value} for item in found],
    )


_Handler = Callable[[RobotToolkit, Any, "str | None"], Awaitable[ToolOutcome]]

_HANDLERS: dict[str, _Handler] = {
    "robot_move": _move,
    "robot_turn": _turn,
    "robot_stop": _stop,
    "robot_look_at": _look_at,
    "robot_follow_person": _follow_person,
    "robot_stop_following": _stop_following,
    "robot_play_animation": _play_animation,
    "robot_set_expression": _set_expression,
    "robot_capture_image": _capture_image,
    "robot_inspect_object": _inspect_object,
    "robot_get_battery": _get_battery,
    "robot_get_state": _get_state,
    "robot_remember": _remember,
    "robot_recall": _recall,
}


def _nearest_person(kit: RobotToolkit) -> str | None:
    world = kit.runtime.world.snapshot(kit.robot_id)
    nearest = world.nearest(EntityType.PERSON)
    return nearest.id if nearest is not None else None


def _seen(record: ActionRecord | None) -> str:
    """The device's own description of the frame, or an empty string."""
    result = record.result if record is not None else None
    if not isinstance(result, dict):
        return ""
    for key in ("answer", "description", "text", "result"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


__all__ = [
    "DEFAULT_TOOL_TIMEOUT_S",
    "NEAREST",
    "TOOLS_BY_NAME",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "RobotToolSpec",
    "RobotToolkit",
    "ToolArguments",
    "ToolError",
    "ToolOutcome",
]
