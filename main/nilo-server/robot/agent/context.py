"""What the model is told before it answers, and what it is deliberately not told.

    context = await build_context(runtime, "nilo-sim-01", person_id="ahmad", query="come closer")
    system_prompt = context.render()

Eight blocks, each of them one or two lines:

``identity``      who this robot is
``state``         what it is doing *right now* — one value per field, never a series
``world``         who and what it can see, with ages in seconds
``activity``      what the behaviour engine is running, if anything
``person``        who is speaking, and how well the robot knows them
``memory``        the bounded, ranked selection :mod:`robot.memory` already produces
``tools``         the tools that are actually usable on this turn, not the whole catalogue
``restrictions``  what is refused right now, and why

**No raw telemetry history.** The world model holds a current snapshot, not a series, and
this module reads only that snapshot. A context that grows with uptime is a context that
eventually costs more than the answer, and a model given four hundred battery readings
does not reason better about the battery — it reasons about the readings. A test asserts
the rendered context is bounded (:func:`approx_tokens`).

**The tool list is the permitted list.** Offering a model a tool and then refusing it is
how a robot ends up apologising for something it was never going to do. The context names
what :class:`~robot.agent.permissions.ToolPolicy` would actually allow on this turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from robot.agent.permissions import TurnOrigin
from robot.state.world import Entity, WorldState

logger = logging.getLogger(__name__)

#: How many characters of memory context to carry. Retrieval is already token-budgeted;
#: this is the second, cruder bound that holds when somebody configures a large budget.
MAX_MEMORY_CHARS = 1500

#: How many visible entities to name. Beyond this the list stops being information.
MAX_ENTITIES = 6

#: How old an entity may be and still be worth naming, in seconds.
MAX_ENTITY_AGE_S = 60.0


@dataclass(frozen=True, slots=True)
class PersonContext:
    """Who is talking, as far as the robot knows."""

    person_id: str | None = None
    display_name: str = ""
    familiarity: float = 0.0
    interactions: int = 0

    @property
    def known(self) -> bool:
        return self.person_id is not None

    def render(self) -> str:
        if not self.known:
            return "Speaking to: somebody the robot does not recognise."
        name = self.display_name or self.person_id
        return (
            f"Speaking to: {name} (id {self.person_id}), "
            f"met {self.interactions} time(s), familiarity {self.familiarity:.2f}."
        )


@dataclass(frozen=True, slots=True)
class Restrictions:
    """What the robot may not do right now, in the model's own terms."""

    emergency_stopped: bool = False
    connected: bool = True
    autonomy_mode: str = "normal"
    hazards: tuple[str, ...] = ()
    origin: TurnOrigin = TurnOrigin.USER

    def render(self) -> str:
        lines = [f"Autonomy mode: {self.autonomy_mode}."]
        if self.emergency_stopped:
            lines.append("Emergency stop is engaged: nothing will move until it is cleared.")
        if not self.connected:
            lines.append("The robot is not connected: no command will reach it.")
        if self.hazards:
            lines.append("Sensors report: " + ", ".join(self.hazards) + ". Motion may be refused.")
        lines.append(
            "Safety is decided below you. If a tool comes back refused, say so plainly "
            "and do not try another way to achieve the same movement."
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class RobotContext:
    """The whole runtime context, as data. :meth:`render` is the string a model sees."""

    robot_id: str
    name: str = "robot"
    hardware_model: str = "unknown"
    activity: str = "idle"
    behavior: str | None = None
    connected: bool = True
    battery_percent: int | None = None
    charging: bool = False
    moving: bool = False
    expression: str = "neutral"
    entities: tuple[str, ...] = ()
    environment: str = ""
    person: PersonContext = field(default_factory=PersonContext)
    memories: str = ""
    tools: tuple[str, ...] = ()
    restrictions: Restrictions = field(default_factory=Restrictions)

    def render(self) -> str:
        """The system-prompt block. Short sentences, current values, no history."""
        blocks = [
            f"You are {self.name}, a small social robot (id {self.robot_id}, "
            f"hardware {self.hardware_model}). You have a body, and you are in the room "
            f"with the person you are talking to.",
            self._state_block(),
            self._world_block(),
            self.person.render(),
        ]
        if self.memories:
            blocks.append("What the robot remembers:\n" + self.memories[:MAX_MEMORY_CHARS])
        blocks.append(
            "Tools you can use on this turn: "
            + (", ".join(self.tools) if self.tools else "none — answer with words only.")
        )
        blocks.append(self.restrictions.render())
        blocks.append(
            "Keep spoken replies to one or two sentences. Call a tool when the person "
            "asks for something physical; do not narrate an action you did not call."
        )
        return "\n\n".join(block for block in blocks if block)

    def as_dict(self) -> dict[str, Any]:
        """The same context as JSON-able data, for the management API and the dashboard."""
        return {
            "robot_id": self.robot_id,
            "name": self.name,
            "hardware_model": self.hardware_model,
            "activity": self.activity,
            "behavior": self.behavior,
            "connected": self.connected,
            "battery_percent": self.battery_percent,
            "charging": self.charging,
            "moving": self.moving,
            "expression": self.expression,
            "entities": list(self.entities),
            "environment": self.environment,
            "person": {
                "person_id": self.person.person_id,
                "display_name": self.person.display_name,
                "familiarity": self.person.familiarity,
                "interactions": self.person.interactions,
            },
            "memories": self.memories,
            "tools": list(self.tools),
            "restrictions": {
                "emergency_stopped": self.restrictions.emergency_stopped,
                "connected": self.restrictions.connected,
                "autonomy_mode": self.restrictions.autonomy_mode,
                "hazards": list(self.restrictions.hazards),
                "origin": self.restrictions.origin.value,
            },
        }

    def approx_tokens(self) -> int:
        """A rough size, for the bound a test asserts. Four characters to a token."""
        return len(self.render()) // 4

    def _state_block(self) -> str:
        parts = [f"Right now: {self.activity}"]
        if self.behavior:
            parts.append(f"running the {self.behavior} behaviour")
        if self.moving:
            parts.append("moving")
        if self.battery_percent is not None:
            parts.append(f"battery {self.battery_percent}%{' and charging' if self.charging else ''}")
        parts.append(f"face showing {self.expression}")
        return ". ".join([", ".join(parts)]) + "."

    def _world_block(self) -> str:
        if not self.entities:
            seen = "The robot cannot see anybody or anything it recognises."
        else:
            seen = "The robot can see: " + "; ".join(self.entities) + "."
        return f"{seen} {self.environment}".strip()


async def build_context(
    runtime: Any,
    robot_id: str,
    *,
    person_id: str | None = None,
    query: str = "",
    tools: tuple[str, ...] = (),
    origin: TurnOrigin = TurnOrigin.USER,
    memory: Any = None,
) -> RobotContext:
    """Assemble the context for one turn. Never raises: a missing piece is left out.

    Every lookup is defensive on purpose. This runs on the path between somebody speaking
    and the robot answering, and a robot that cannot answer because its world model has no
    entry for the battery is worse than a robot that answers without knowing.
    """
    state = await _safely(runtime.get_state(robot_id))
    world = _snapshot(runtime, robot_id)
    telemetry = getattr(state, "telemetry", None)
    identity = getattr(state, "identity", None)
    battery = getattr(telemetry, "battery", None)
    motion = getattr(telemetry, "motion", None)
    activity_state = getattr(telemetry, "activity", None)
    expression = getattr(telemetry, "expression", None)

    person = await _person_context(memory, person_id)
    memories = ""
    if memory is not None:
        remembered = await _safely(memory.context_for(query, person_id=person_id))
        memories = (remembered or "")[:MAX_MEMORY_CHARS]

    return RobotContext(
        robot_id=robot_id,
        name=getattr(identity, "name", None) or "robot",
        hardware_model=getattr(identity, "hardware_model", None) or "unknown",
        activity=getattr(getattr(activity_state, "activity", None), "value", None) or "idle",
        behavior=_running_behavior(runtime, robot_id),
        connected=bool(getattr(state, "is_connected", False)),
        battery_percent=getattr(battery, "percent", None),
        charging=bool(getattr(battery, "charging", False)),
        moving=bool(getattr(motion, "moving", False)),
        expression=getattr(expression, "emotion", None) or "neutral",
        entities=_entities(world),
        environment=_environment(world),
        person=person,
        memories=memories,
        tools=tools,
        restrictions=_restrictions(runtime, robot_id, state, world, origin),
    )


# -- pieces ------------------------------------------------------------------------------------


async def _safely(awaitable: Any) -> Any:
    try:
        return await awaitable
    except Exception as exc:  # a context that cannot be built must still be a context
        logger.warning("robot context: a lookup failed and was left out: %s", exc)
        return None


def _snapshot(runtime: Any, robot_id: str) -> WorldState:
    try:
        snapshot: WorldState = runtime.world.snapshot(robot_id)
        return snapshot
    except Exception:
        return WorldState(robot_id=robot_id)


def _entities(world: WorldState) -> tuple[str, ...]:
    fresh = [
        entity
        for entity in world.entities.values()
        if entity.age_s(world.updated_at) <= MAX_ENTITY_AGE_S
    ]
    fresh.sort(key=lambda entity: entity.age_s(world.updated_at))
    return tuple(_describe(entity, world) for entity in fresh[:MAX_ENTITIES])


def _describe(entity: Entity, world: WorldState) -> str:
    """One entity in one clause: what it is, roughly where, and how stale."""
    age = entity.age_s(world.updated_at)
    label = str(entity.attributes.get("label") or entity.attributes.get("name") or entity.type.value)
    parts = [label, _bearing(entity)]
    distance = getattr(entity.position, "distance_mm", 0) if entity.position is not None else 0
    if distance:
        parts.append(f"about {distance / 1000:.1f}m away")
    parts.append(f"seen {age:.0f}s ago")
    if world.attention_target == entity.id:
        parts.append("being looked at")
    return ", ".join(part for part in parts if part)


def _bearing(entity: Entity) -> str:
    """Left, right or ahead, from the image point when there is one and the bearing otherwise."""
    point = entity.image_point
    if point is not None:
        if point.x < 0.4:
            return "to the left"
        return "to the right" if point.x > 0.6 else "ahead"
    if entity.position is None:
        return ""
    bearing = entity.position.bearing_deg
    if bearing < -10:
        return "to the right"
    return "to the left" if bearing > 10 else "ahead"


def _environment(world: WorldState) -> str:
    label = getattr(world.environment, "label", "") or ""
    return f"The robot believes it is in the {label}." if label and label != "unknown" else ""


def _running_behavior(runtime: Any, robot_id: str) -> str | None:
    try:
        engine = runtime.behavior(robot_id)
    except Exception:
        return None
    running: str | None = getattr(getattr(engine, "scheduler", None), "running", None)
    return running


def _restrictions(
    runtime: Any, robot_id: str, state: Any, world: WorldState, origin: TurnOrigin
) -> Restrictions:
    hazards = []
    sensors = getattr(getattr(state, "telemetry", None), "sensors", None)
    if getattr(sensors, "cliff_detected", False):
        hazards.append("a drop in front of the robot")
    if getattr(sensors, "bump_detected", False):
        hazards.append("something bumped into")
    if getattr(sensors, "picked_up", False):
        hazards.append("the robot has been picked up")
    if world.blocked:
        hazards.append("an obstacle close ahead")
    estop = False
    actions = getattr(runtime, "actions", None)
    if actions is not None:
        try:
            estop = bool(actions.emergency_stopped(robot_id))
        except Exception:
            estop = False
    mode = getattr(getattr(runtime, "autonomy", None), "value", "normal")
    return Restrictions(
        emergency_stopped=estop,
        connected=bool(getattr(state, "is_connected", False)),
        autonomy_mode=str(mode),
        hazards=tuple(hazards),
        origin=origin,
    )


async def _person_context(memory: Any, person_id: str | None) -> PersonContext:
    if person_id is None:
        return PersonContext()
    if memory is None:
        return PersonContext(person_id=person_id)
    record = await _safely(memory.person(person_id))
    if record is None:
        return PersonContext(person_id=person_id)
    return PersonContext(
        person_id=person_id,
        display_name=getattr(record, "display_name", "") or "",
        familiarity=float(record.familiarity_now()),
        interactions=int(getattr(record, "interaction_count", 0)),
    )


__all__ = [
    "MAX_ENTITIES",
    "MAX_ENTITY_AGE_S",
    "MAX_MEMORY_CHARS",
    "PersonContext",
    "Restrictions",
    "RobotContext",
    "build_context",
]
