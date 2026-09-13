"""The vocabulary of the behaviour engine: what a behaviour is and what it is given.

A behaviour answers three questions and does one thing:

    can_run(context)   is this even applicable right now?      (a hard filter)
    score(context)     how much is it worth, 0..1?             (a utility)
    execute(context)   do it, using the semantic action API    (the only side effects)
    cancel()           stop doing it, now                      (preemption, e-stop)

plus the declarations the scheduler arbitrates on: ``priority``, ``cooldown_s``,
``required_resources``, ``min_runtime_s``, ``max_runtime_s``, ``interruptible`` and
``min_autonomy``.

Two rules are structural rather than conventional:

* **Scoring is pure.** ``score`` reads the context and returns a number. It must not
  await, must not call the robot, and must not read a clock — the only time it may see is
  ``context.now``, and the only randomness ``context.rng``. That is what makes a fixed
  world plus a fixed seed produce the same decision every time.
* **Safety is not negotiable here.** A behaviour proposes; the action layer disposes. A
  score of 1.0 buys nothing from the safety policy (docs/safety.md), which is one layer
  down and cannot see this module.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Protocol, runtime_checkable

from robot.behavior.tuning import BehaviorTuning
from robot.state.actions import ActionRecord, Resource
from robot.state.world import WorldState


class AutonomyMode(str, Enum):
    """How much the robot is allowed to start on its own.

    The modes are a containment boundary, not a personality setting: they bound what the
    engine may *initiate*. A direct command from a user or the LLM goes through the action
    layer and is unaffected by the mode.
    """

    #: Nothing autonomous. Direct commands only.
    OFF = "off"
    #: Reactive expressions — the face and the speaker — but nothing that moves.
    PASSIVE = "passive"
    #: Everyday autonomy: looking around, reacting, greeting, docking.
    NORMAL = "normal"
    #: Everything, including driving off on its own: exploring, approaching, following.
    FULL = "full"

    @property
    def rank(self) -> int:
        return _MODE_RANK[self]

    def allows(self, behavior: Behavior) -> bool:
        """Whether this mode permits the engine to start ``behavior``."""
        if self is AutonomyMode.OFF:
            return False
        if self.rank < behavior.min_autonomy.rank:
            return False
        if self is AutonomyMode.PASSIVE:
            # "Reactive expressions but no autonomous movement": the face and the speaker
            # are expression, everything else is a motor.
            return behavior.required_resources <= EXPRESSIVE_RESOURCES
        return True


_MODE_RANK: dict[AutonomyMode, int] = {
    AutonomyMode.OFF: 0,
    AutonomyMode.PASSIVE: 1,
    AutonomyMode.NORMAL: 2,
    AutonomyMode.FULL: 3,
}

#: The resources a PASSIVE robot may still claim: the face and the speaker.
EXPRESSIVE_RESOURCES: frozenset[Resource] = frozenset({Resource.DISPLAY, Resource.AUDIO})


class BehaviorCategory(str, Enum):
    """What kind of thing a behaviour is. Ranked, and the ranking is the override rule.

    Safety and system behaviours outrank social and entertainment ones *categorically* —
    a docking run at 8% battery is not compared against a greeting on utility, it wins
    because of what it is (docs/robot-behavior.md).
    """

    SAFETY = "safety"
    SYSTEM = "system"
    SOCIAL = "social"
    EXPLORATION = "exploration"
    ENTERTAINMENT = "entertainment"
    IDLE = "idle"


class BehaviorPriority(IntEnum):
    """The band a behaviour is arbitrated in. Higher wins before utility is consulted."""

    IDLE = 0
    LOW = 10
    NORMAL = 50
    HIGH = 80
    #: Reserved for safety and power: the band nothing social may enter.
    CRITICAL = 100


class BehaviorOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class BehaviorResult:
    """What running a behaviour achieved. Returned by :meth:`Behavior.execute`."""

    outcome: BehaviorOutcome = BehaviorOutcome.COMPLETED
    detail: str = ""
    actions: tuple[ActionRecord, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.outcome is BehaviorOutcome.COMPLETED

    @classmethod
    def completed(cls, detail: str = "", *actions: ActionRecord | None) -> BehaviorResult:
        return cls(BehaviorOutcome.COMPLETED, detail, tuple(a for a in actions if a is not None))

    @classmethod
    def failed(cls, detail: str, *actions: ActionRecord | None) -> BehaviorResult:
        return cls(BehaviorOutcome.FAILED, detail, tuple(a for a in actions if a is not None))

    @classmethod
    def skipped(cls, detail: str = "") -> BehaviorResult:
        return cls(BehaviorOutcome.SKIPPED, detail, ())


class Drives(Protocol):
    """The internal control variables scoring may read.

    **Not emotions.** They are normalized 0..1 numbers that bias a utility function, named
    after the thing they bias so the scoring code reads as intended. Phase 5 supplies a
    real implementation that decays over time (``robot/personality/``); until then
    :class:`NeutralDrives` holds everything at its midpoint and every behaviour still runs.
    """

    @property
    def curiosity(self) -> float: ...

    @property
    def boredom(self) -> float: ...

    @property
    def social_need(self) -> float: ...

    @property
    def energy(self) -> float: ...

    @property
    def valence(self) -> float: ...

    @property
    def arousal(self) -> float: ...

    @property
    def confidence(self) -> float: ...


@dataclass(frozen=True)
class NeutralDrives:
    """Every drive at its midpoint. The default, and what a test uses when it wants none."""

    curiosity: float = 0.5
    boredom: float = 0.0
    social_need: float = 0.5
    energy: float = 1.0
    valence: float = 0.5
    arousal: float = 0.3
    confidence: float = 0.7


@runtime_checkable
class RobotCommands(Protocol):
    """The slice of the semantic action API a behaviour is allowed to use.

    A structural type rather than an import of
    :class:`~robot.actions.semantic.RobotHandle`: it documents exactly what behaviours may
    command (no raw actuator is reachable from here), and it lets a test hand in a
    recorder instead of an executor.
    """

    async def move(self, distance_mm: int, speed_mmps: int = ..., **kwargs: Any) -> ActionRecord: ...

    async def turn(self, angle_deg: int, speed_dps: int = ..., **kwargs: Any) -> ActionRecord: ...

    async def stop(self, reason: str = ..., **kwargs: Any) -> ActionRecord: ...

    async def look_at(self, x_pct: int = ..., y_pct: int = ..., **kwargs: Any) -> ActionRecord: ...

    async def head_angle(self, pitch_deg: int = ..., yaw_deg: int = ..., **kwargs: Any) -> ActionRecord: ...

    async def set_expression(self, emotion: str, intensity_pct: int = ..., **kwargs: Any) -> ActionRecord: ...

    # Optional, because an animation can be refused — the head is busy, the robot is too
    # tired for an energetic one — and a refusal is an outcome rather than an error.
    async def play_animation(self, name: str, **kwargs: Any) -> ActionRecord | None: ...

    async def follow(self, target_id: str, **kwargs: Any) -> ActionRecord: ...


class BehaviorMemory:
    """Timestamps a behaviour needs between ticks, keyed by a string it makes up.

    Deliberately not per-behaviour state on the instance: behaviours are stateless and
    shared, and "when did I last greet *this person*" is keyed by more than the behaviour
    name. Times are scheduler time (seconds from its clock), not wall clock.
    """

    __slots__ = ("_marks",)

    def __init__(self) -> None:
        self._marks: dict[str, float] = {}

    def mark(self, key: str, now: float) -> None:
        self._marks[key] = now

    def at(self, key: str) -> float | None:
        return self._marks.get(key)

    def since(self, key: str, now: float) -> float:
        """Seconds since ``key`` was marked, or ``inf`` if it never was."""
        marked = self._marks.get(key)
        return float("inf") if marked is None else now - marked

    def forget(self, key: str) -> None:
        self._marks.pop(key, None)

    def clear(self) -> None:
        self._marks.clear()

    def __len__(self) -> int:
        return len(self._marks)


@dataclass
class BehaviorContext:
    """Everything a behaviour may see. Built fresh by the scheduler on every tick.

    ``now`` is the scheduler's clock in seconds — monotonic, and in tests a number a test
    controls. Nothing in a behaviour may read the wall clock, which is what makes the
    suite run in milliseconds and makes a decision replayable.
    """

    robot_id: str
    world: WorldState
    now: float
    mode: AutonomyMode
    tuning: BehaviorTuning
    robot: RobotCommands
    rng: random.Random
    drives: Drives = field(default_factory=NeutralDrives)
    memory: BehaviorMemory = field(default_factory=BehaviorMemory)
    last_run: Mapping[str, float] = field(default_factory=dict)
    running: str | None = None
    reasons: list[str] = field(default_factory=list)
    #: Scheduler time at which the engine started. The idle clock runs from here until
    #: the first interaction gives it something better to measure from.
    started_at: float = 0.0
    #: Scheduler time of the last stimulus worth waking up for (touch, sound, a person).
    last_stimulus_at: float | None = None

    def because(self, reason: str) -> None:
        """Record why the score being computed is what it is. Shows up in ``explain``."""
        self.reasons.append(reason)

    def take_reasons(self) -> tuple[str, ...]:
        collected = tuple(self.reasons)
        self.reasons.clear()
        return collected

    def since_run(self, name: str) -> float:
        """Seconds since a behaviour last *finished*, or ``inf`` if it never has."""
        last = self.last_run.get(name)
        return float("inf") if last is None else self.now - last

    def idle_s(self) -> float:
        """Seconds since the last interaction ended, or since the engine started.

        The input to boredom and to sleep. An open interaction means zero: the robot is
        not idle while somebody is talking to it.
        """
        interaction = self.world.current_interaction
        if interaction is not None and interaction.is_open:
            return 0.0
        last = self.world.last_interaction
        if last is not None and last.ended_at is not None:
            return max(0.0, (self.world.updated_at - last.ended_at).total_seconds())
        return max(0.0, self.now - self.started_at)

    def since_stimulus(self) -> float:
        """Seconds since the last thing worth reacting to. ``inf`` if there has been none."""
        if self.last_stimulus_at is None:
            return float("inf")
        return self.now - self.last_stimulus_at


class Behavior(ABC):
    """One thing the robot can decide to do on its own.

    Subclasses set the class attributes they differ on and implement :meth:`score` and
    :meth:`execute`. Defaults are the common case: interruptible, no cooldown, no
    resources, normal priority, available from ``NORMAL`` autonomy upward.
    """

    #: Stable snake_case identifier. Appears in events, logs and the explain output.
    name: str = "behavior"
    category: BehaviorCategory = BehaviorCategory.IDLE
    priority: BehaviorPriority = BehaviorPriority.NORMAL
    #: Subsystems this behaviour commands while it runs. Two behaviours that claim the
    #: same resource cannot run at once — the same rule the action queue applies.
    required_resources: frozenset[Resource] = frozenset()
    #: Seconds after finishing before this behaviour may be selected again.
    cooldown_s: float = 0.0
    #: Lowest autonomy mode that may start it.
    min_autonomy: AutonomyMode = AutonomyMode.NORMAL
    #: Whether a better candidate may preempt it once ``min_runtime_s`` has passed.
    interruptible: bool = True
    min_runtime_s: float | None = None
    max_runtime_s: float | None = None

    def can_run(self, context: BehaviorContext) -> bool:
        """A hard applicability filter, evaluated before scoring. Cheap and side-effect free."""
        return True

    @abstractmethod
    def score(self, context: BehaviorContext) -> float:
        """Utility in 0..1. Pure: no awaits, no clocks, no side effects beyond
        :meth:`BehaviorContext.because`."""

    @abstractmethod
    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        """Do the thing, through ``context.robot``. May be cancelled at any await point."""

    async def cancel(self) -> None:
        """Stop early. Called on preemption, on an overrun, and at shutdown.

        The default is a no-op because the scheduler cancels the execute task itself; a
        behaviour that holds something the task cancellation will not release (a device
        follow, a long animation) overrides this and lets go of it.
        """
        return None

    def cooldown(self, tuning: BehaviorTuning) -> float:
        """Seconds before this behaviour may be selected again.

        A method rather than only the attribute so a behaviour whose cooldown belongs in
        the tuning file can name it there (``return tuning.greet_cooldown_s``) instead of
        carrying a copy of the number.
        """
        return self.cooldown_s

    def min_runtime(self, tuning: BehaviorTuning) -> float:
        return tuning.default_min_runtime_s if self.min_runtime_s is None else self.min_runtime_s

    def max_runtime(self, tuning: BehaviorTuning) -> float:
        return tuning.default_max_runtime_s if self.max_runtime_s is None else self.max_runtime_s

    def conflicts_with(self, other: Behavior) -> bool:
        return bool(self.required_resources & other.required_resources)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} {self.category.value} p{int(self.priority)}>"


__all__ = [
    "EXPRESSIVE_RESOURCES",
    "AutonomyMode",
    "Behavior",
    "BehaviorCategory",
    "BehaviorContext",
    "BehaviorMemory",
    "BehaviorOutcome",
    "BehaviorPriority",
    "BehaviorResult",
    "Drives",
    "NeutralDrives",
    "RobotCommands",
]
