"""The world model: what the robot believes is around it right now.

:class:`RobotState` (``models.py``) is what the *robot* reports about itself. This module
is the other half — what the robot believes about everything else: people, faces, objects,
locations, obstacles, who it is paying attention to, and which interaction is in flight.

Three properties are load-bearing:

* **It is a cache, and it says so.** Every entity carries ``first_seen``, ``last_seen`` and
  a ``confidence``. Perception is allowed to be wrong and is always late, so a reader asks
  for fresh entities (:meth:`WorldState.people_seen_within`) rather than assuming the map
  is true. Entities decay out on a documented schedule (:meth:`WorldState.decay`).
* **It is frozen.** A :class:`WorldState` is an immutable snapshot; every mutation returns
  a new one. A behaviour scored against a snapshot cannot be raced by a perception update
  arriving halfway through the scoring pass, which is what makes deterministic scoring
  possible at all. The freeze is pydantic's, so it is one level deep: the fields cannot be
  reassigned, and every mutator here rebuilds the entity dict rather than writing into it,
  so no two snapshots share one. Reaching into ``world.entities`` and assigning is the one
  way to break that, and nothing in the tree does it.
* **Events write it, nothing else does.** :class:`WorldModel` subscribes to the event bus
  and folds events into snapshots. Readers (the behaviour engine, the management API) only
  read. There is no second writer.

Coordinates come in two flavours and they are never mixed. :class:`Position` is metric and
robot-relative (millimetres, degrees), for things the robot could drive to.
:class:`ImagePoint` is normalized 0.0-1.0 image space, for things a camera saw
(docs/robot-architecture.md Sect. 2.4), and is resolution-independent by construction.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robot.state.models import RobotTelemetry, Timestamped, utcnow


class EntityType(str, Enum):
    """What kind of thing an entity is. The world is indexed by this."""

    ROBOT = "robot"
    PERSON = "person"
    FACE = "face"
    OBJECT = "object"
    LOCATION = "location"
    OBSTACLE = "obstacle"


class ImagePoint(BaseModel):
    """A point in normalized image space: ``0,0`` top-left, ``1,1`` bottom-right.

    Normalized so a tracker written against a 160x120 simulated frame keeps working on a
    1600x1200 one, and so "the person is on the left" is a comparison against ``0.5``
    rather than against a resolution nobody recorded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(default=0.0, ge=0.0, le=1.0)
    height: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def area(self) -> float:
        """Fraction of the frame the box covers. The only depth cue a mono camera gives."""
        return self.width * self.height

    def offset_from_centre(self) -> tuple[float, float]:
        """How far off-centre, in the range -0.5..0.5. What a look-at controller drives to zero."""
        return (self.x - 0.5, self.y - 0.5)


class Position(BaseModel):
    """Where something is, relative to the robot. Millimetres and degrees."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    distance_mm: int = Field(default=0, ge=0)
    bearing_deg: float = 0.0
    x_mm: int | None = None
    y_mm: int | None = None
    frame: str = "robot"


class Entity(Timestamped):
    """One thing the robot believes exists.

    ``attributes`` is deliberately open: a face carries an embedding reference, an object
    carries a label and a detector score, a location carries a name. Putting them in a
    typed subclass per kind would mean a new class every time perception learns a field,
    and none of the readers care.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    type: EntityType
    attributes: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    position: Position | None = None
    image_point: ImagePoint | None = None

    def age_s(self, now: datetime | None = None) -> float:
        """Seconds since this entity was last observed."""
        return ((now or utcnow()) - self.last_seen).total_seconds()

    def lifetime_s(self, now: datetime | None = None) -> float:
        """Seconds since it was first observed. How long it has been around."""
        return ((now or utcnow()) - self.first_seen).total_seconds()

    def seen(
        self,
        *,
        now: datetime | None = None,
        confidence: float | None = None,
        position: Position | None = None,
        image_point: ImagePoint | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Entity:
        """The same entity, observed again. ``first_seen`` is never moved forward."""
        moment = now or utcnow()
        merged = dict(self.attributes)
        if attributes:
            merged.update(attributes)
        return self.model_copy(
            update={
                "last_seen": moment,
                "updated_at": moment,
                "confidence": self.confidence if confidence is None else confidence,
                "position": self.position if position is None else position,
                "image_point": self.image_point if image_point is None else image_point,
                "attributes": merged,
            }
        )

    def decayed(self, half_life_s: float, now: datetime | None = None) -> Entity:
        """Confidence after exponential decay. An entity nobody has seen is less believable.

        Halving rather than dropping to zero: a person who stepped behind a chair is still
        probably there, and a behaviour that wants certainty asks for a recent sighting.
        """
        if half_life_s <= 0:
            return self
        factor = 0.5 ** (self.age_s(now) / half_life_s)
        return self.model_copy(update={"confidence": max(0.0, min(1.0, self.confidence * factor))})


class InteractionKind(str, Enum):
    CONVERSATION = "conversation"
    GREETING = "greeting"
    PLAY = "play"
    TOUCH = "touch"
    COMMAND = "command"


class Interaction(BaseModel):
    """One exchange with a person, open or finished.

    ``person_id`` is optional because the robot can be talking to somebody it has not
    recognized — an interaction with an unknown person is still an interaction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: InteractionKind = InteractionKind.CONVERSATION
    person_id: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    turns: int = 0
    summary: str = ""

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def duration_s(self, now: datetime | None = None) -> float:
        return ((self.ended_at or now or utcnow()) - self.started_at).total_seconds()

    def since_end_s(self, now: datetime | None = None) -> float | None:
        """Seconds since it ended, or ``None`` while it is still open."""
        if self.ended_at is None:
            return None
        return ((now or utcnow()) - self.ended_at).total_seconds()


class Environment(Timestamped):
    """Ambient conditions. Weak signals, all of them optional."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sound_level: float = Field(default=0.0, ge=0.0, le=1.0)
    sound_direction_deg: float | None = None
    light_level: float | None = Field(default=None, ge=0.0, le=1.0)
    label: str = "unknown"


#: How long an entity of each kind survives without being seen again, in seconds.
#: A person who walked out is gone in twenty seconds; a location the robot learned is
#: remembered for an hour, because furniture does not move when nobody is looking.
DEFAULT_TTL_S: dict[EntityType, float] = {
    EntityType.ROBOT: 60.0,
    EntityType.PERSON: 20.0,
    EntityType.FACE: 10.0,
    EntityType.OBJECT: 60.0,
    EntityType.OBSTACLE: 30.0,
    EntityType.LOCATION: 3600.0,
}

#: Confidence half-life per kind. Separate from the TTL: an entity can still be believed
#: in (above zero confidence) well before it is forgotten.
DEFAULT_HALF_LIFE_S: dict[EntityType, float] = {
    EntityType.ROBOT: 30.0,
    EntityType.PERSON: 8.0,
    EntityType.FACE: 4.0,
    EntityType.OBJECT: 30.0,
    EntityType.OBSTACLE: 10.0,
    EntityType.LOCATION: 1800.0,
}


class WorldState(Timestamped):
    """One robot's picture of the world. Immutable; every change returns a new snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    robot_id: str
    entities: dict[str, Entity] = Field(default_factory=dict)
    attention_target: str | None = None
    current_interaction: Interaction | None = None
    last_interaction: Interaction | None = None
    environment: Environment = Field(default_factory=Environment)
    telemetry: RobotTelemetry = Field(default_factory=RobotTelemetry)

    # -- indexed views ------------------------------------------------------------------

    def of_type(self, kind: EntityType) -> tuple[Entity, ...]:
        """Every entity of one kind, newest sighting first."""
        return tuple(
            sorted(
                (entity for entity in self.entities.values() if entity.type is kind),
                key=lambda entity: entity.last_seen,
                reverse=True,
            )
        )

    @property
    def robots(self) -> tuple[Entity, ...]:
        return self.of_type(EntityType.ROBOT)

    @property
    def people(self) -> tuple[Entity, ...]:
        return self.of_type(EntityType.PERSON)

    @property
    def faces(self) -> tuple[Entity, ...]:
        return self.of_type(EntityType.FACE)

    @property
    def objects(self) -> tuple[Entity, ...]:
        return self.of_type(EntityType.OBJECT)

    @property
    def locations(self) -> tuple[Entity, ...]:
        return self.of_type(EntityType.LOCATION)

    @property
    def obstacles(self) -> tuple[Entity, ...]:
        return self.of_type(EntityType.OBSTACLE)

    def get(self, entity_id: str) -> Entity | None:
        return self.entities.get(entity_id)

    def seen_within(self, kind: EntityType, max_age_s: float, now: datetime | None = None) -> tuple[Entity, ...]:
        """Entities of a kind observed recently enough to act on."""
        moment = now or utcnow()
        return tuple(entity for entity in self.of_type(kind) if entity.age_s(moment) <= max_age_s)

    def nearest(self, kind: EntityType, now: datetime | None = None) -> Entity | None:
        """The closest entity of a kind that has a position, or the newest one if none do."""
        candidates = self.of_type(kind)
        if not candidates:
            return None
        located = [entity for entity in candidates if entity.position is not None]
        if not located:
            return candidates[0]
        return min(located, key=lambda entity: entity.position.distance_mm if entity.position else 0)

    @property
    def attention(self) -> Entity | None:
        return self.entities.get(self.attention_target) if self.attention_target else None

    # -- telemetry shortcuts, so a behaviour does not re-derive them ----------------------

    @property
    def battery_percent(self) -> int | None:
        battery = self.telemetry.battery
        return None if battery is None else battery.percent

    @property
    def charging(self) -> bool:
        battery = self.telemetry.battery
        return bool(battery is not None and battery.charging)

    @property
    def touched(self) -> bool:
        sensors = self.telemetry.sensors
        return bool(sensors is not None and sensors.touch_detected)

    @property
    def blocked(self) -> bool:
        sensors = self.telemetry.sensors
        return bool(sensors is not None and sensors.blocked)

    @property
    def moving(self) -> bool:
        motion = self.telemetry.motion
        return bool(motion is not None and motion.moving)

    # -- mutation (each returns a new snapshot) --------------------------------------------

    def observe(self, entity: Entity, now: datetime | None = None) -> WorldState:
        """Record a sighting. Merges into an existing entity of the same id, or adds it."""
        moment = now or utcnow()
        existing = self.entities.get(entity.id)
        if existing is not None and existing.type is entity.type:
            updated = existing.seen(
                now=entity.last_seen,
                confidence=entity.confidence,
                position=entity.position,
                image_point=entity.image_point,
                attributes=entity.attributes,
            )
        else:
            updated = entity
        entities = dict(self.entities)
        entities[entity.id] = updated
        return self.model_copy(update={"entities": entities, "updated_at": moment})

    def forget(self, entity_id: str, now: datetime | None = None) -> WorldState:
        """Drop one entity. Clears the attention target if that is what it pointed at."""
        if entity_id not in self.entities:
            return self
        entities = dict(self.entities)
        del entities[entity_id]
        update: dict[str, Any] = {"entities": entities, "updated_at": now or utcnow()}
        if self.attention_target == entity_id:
            update["attention_target"] = None
        return self.model_copy(update=update)

    def attend_to(self, entity_id: str | None, now: datetime | None = None) -> WorldState:
        """Point attention at an entity (or at nothing). Unknown ids are refused loudly."""
        if entity_id is not None and entity_id not in self.entities:
            raise KeyError(f"cannot attend to unknown entity {entity_id!r}")
        return self.model_copy(update={"attention_target": entity_id, "updated_at": now or utcnow()})

    def with_telemetry(self, telemetry: RobotTelemetry, now: datetime | None = None) -> WorldState:
        return self.model_copy(
            update={"telemetry": self.telemetry.merge(telemetry), "updated_at": now or utcnow()}
        )

    def with_environment(self, environment: Environment, now: datetime | None = None) -> WorldState:
        return self.model_copy(update={"environment": environment, "updated_at": now or utcnow()})

    def start_interaction(self, interaction: Interaction, now: datetime | None = None) -> WorldState:
        """Open an interaction, closing whatever was open before it."""
        moment = now or utcnow()
        previous = self.current_interaction
        update: dict[str, Any] = {"current_interaction": interaction, "updated_at": moment}
        if previous is not None:
            update["last_interaction"] = (
                previous if not previous.is_open else previous.model_copy(update={"ended_at": moment})
            )
        return self.model_copy(update=update)

    def end_interaction(self, now: datetime | None = None, summary: str = "") -> WorldState:
        """Close the open interaction and move it to ``last_interaction``."""
        if self.current_interaction is None:
            return self
        moment = now or utcnow()
        finished = self.current_interaction.model_copy(
            update={"ended_at": moment, "summary": summary or self.current_interaction.summary}
        )
        return self.model_copy(
            update={"current_interaction": None, "last_interaction": finished, "updated_at": moment}
        )

    def decay(
        self,
        now: datetime | None = None,
        *,
        ttl_s: Mapping[EntityType, float] | None = None,
        half_life_s: Mapping[EntityType, float] | None = None,
    ) -> WorldState:
        """Age every entity: lower confidence, and drop what is past its TTL.

        The documented schedule of :data:`DEFAULT_TTL_S` and :data:`DEFAULT_HALF_LIFE_S`,
        applied in one pass. Called on a timer by :class:`WorldModel` and directly by
        tests, which is why it takes ``now`` rather than reading a clock.
        """
        moment = now or utcnow()
        ttls = dict(DEFAULT_TTL_S) | dict(ttl_s or {})
        lives = dict(DEFAULT_HALF_LIFE_S) | dict(half_life_s or {})
        entities: dict[str, Entity] = {}
        for entity_id, entity in self.entities.items():
            if entity.age_s(moment) > ttls.get(entity.type, math.inf):
                continue
            entities[entity_id] = entity.decayed(lives.get(entity.type, 0.0), moment)
        if entities.keys() == self.entities.keys() and entities == self.entities:
            return self
        attention = self.attention_target if self.attention_target in entities else None
        return self.model_copy(
            update={"entities": entities, "attention_target": attention, "updated_at": moment}
        )


def person(
    entity_id: str,
    *,
    name: str | None = None,
    known: bool = False,
    confidence: float = 1.0,
    position: Position | None = None,
    image_point: ImagePoint | None = None,
    now: datetime | None = None,
    **attributes: Any,
) -> Entity:
    """A person entity, with the attributes the social behaviours read by name."""
    moment = now or utcnow()
    payload: dict[str, Any] = {"known": known, **attributes}
    if name is not None:
        payload["name"] = name
    return Entity(
        id=entity_id,
        type=EntityType.PERSON,
        attributes=payload,
        confidence=confidence,
        first_seen=moment,
        last_seen=moment,
        updated_at=moment,
        position=position,
        image_point=image_point,
    )


def obj(
    entity_id: str,
    *,
    label: str = "object",
    confidence: float = 1.0,
    position: Position | None = None,
    image_point: ImagePoint | None = None,
    now: datetime | None = None,
    **attributes: Any,
) -> Entity:
    """An object entity. Named ``obj`` because ``object`` is a builtin."""
    moment = now or utcnow()
    return Entity(
        id=entity_id,
        type=EntityType.OBJECT,
        attributes={"label": label, **attributes},
        confidence=confidence,
        first_seen=moment,
        last_seen=moment,
        updated_at=moment,
        position=position,
        image_point=image_point,
    )


def entities_by_id(entities: Iterable[Entity]) -> dict[str, Entity]:
    return {entity.id: entity for entity in entities}


__all__ = [
    "DEFAULT_HALF_LIFE_S",
    "DEFAULT_TTL_S",
    "Entity",
    "EntityType",
    "Environment",
    "ImagePoint",
    "Interaction",
    "InteractionKind",
    "Position",
    "WorldState",
    "entities_by_id",
    "obj",
    "person",
]
