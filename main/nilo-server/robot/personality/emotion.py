"""Internal behaviour-control variables, and how they move.

**These are not emotions.** They are seven normalized numbers that bias a utility
function, named after what they bias so the scoring code reads as intended. The robot does
not feel anything; ``valence`` rising after a greeting means a number went up and some
behaviours became more likely. Every log line, doc sentence and comment in this package is
written that way on purpose — a system that claims feelings it does not have is lying to
its owner (docs/robot-personality.md).

The model:

* **Decay towards a resting point.** Every variable moves exponentially towards a baseline
  derived from the personality traits, with its own half-life. Nothing snaps.
* **Stimuli nudge it.** A stimulus is a named, bounded push: ``positive_interaction``
  raises valence and lowers social need, ``failed_movement`` lowers confidence a little,
  ``obstacle`` raises arousal. The table is data, in :class:`EmotionTuning`.
* **Time is injected.** The engine is told what time it is; it never reads a clock. Decay
  over an hour is a test that runs in a microsecond.

Boredom is the one that rises when nothing happens: its baseline is 1.0, so with no
stimulus it climbs, and any stimulus knocks it back down. Curiosity does the same towards
the personality's curiosity trait. That is the whole "no stimulation: boredom rises,
curiosity rises" rule, expressed as two baselines rather than as a special case.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robot.personality.traits import PersonalityTraits

logger = logging.getLogger(__name__)


class Stimulus(str, Enum):
    """Things that happen to a robot, as far as its control variables are concerned.

    A closed set, because an open one becomes a string typo nobody notices. Each entry
    maps to a row in :attr:`EmotionTuning.nudges`.
    """

    POSITIVE_INTERACTION = "positive_interaction"
    NEGATIVE_INTERACTION = "negative_interaction"
    PRAISE = "praise"
    TOUCH = "touch"
    PERSON_SEEN = "person_seen"
    PERSON_LOST = "person_lost"
    NOVELTY = "novelty"
    CURIOSITY_SATISFIED = "curiosity_satisfied"
    FAILED_MOVEMENT = "failed_movement"
    SUCCESSFUL_MOVEMENT = "successful_movement"
    OBSTACLE = "obstacle"
    LOUD_SOUND = "loud_sound"
    CHARGING = "charging"
    RESTED = "rested"


class EmotionalState(BaseModel):
    """The seven control variables. Frozen: every change produces a new state.

    Satisfies :class:`robot.behavior.base.Drives` structurally, which is how the behaviour
    engine reads it without importing this package.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: How good things are, 0 bad .. 1 good. Biases expression choice more than behaviour.
    valence: float = Field(default=0.55, ge=0.0, le=1.0)
    #: How activated the robot is, 0 calm .. 1 startled. Raises reaction scores.
    arousal: float = Field(default=0.25, ge=0.0, le=1.0)
    #: Appetite for novelty right now. Raises investigating and exploring.
    curiosity: float = Field(default=0.5, ge=0.0, le=1.0)
    #: How long it has been since anything happened, as a number. Raises boredom
    #: behaviours and, mildly, exploration.
    boredom: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Appetite for company. Raises greeting, following and approaching.
    social_need: float = Field(default=0.5, ge=0.0, le=1.0)
    #: How well recent attempts have gone. Falls on a failed motion, recovers with time.
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    #: How much the robot has left, 0 flat .. 1 full. Rises while charging.
    energy: float = Field(default=0.8, ge=0.0, le=1.0)

    def nudged(self, deltas: dict[str, float], strength: float = 1.0) -> EmotionalState:
        """Apply signed deltas, clamped into 0..1. ``strength`` scales the whole push."""
        values = self.model_dump()
        for name, delta in deltas.items():
            if name not in values:
                raise KeyError(f"{name!r} is not a control variable")
            values[name] = _clamp(values[name] + delta * strength)
        return EmotionalState(**values)

    def decayed(
        self, dt_s: float, baselines: dict[str, float], half_lives: dict[str, float]
    ) -> EmotionalState:
        """Move every variable towards its baseline over ``dt_s`` seconds.

        Exponential, per variable: after one half-life the distance to the baseline has
        halved. Nothing here is linear, because a linear approach reaches the baseline and
        then sits exactly on it, which reads as a robot that stopped.
        """
        if dt_s <= 0:
            return self
        values = self.model_dump()
        for name, value in values.items():
            baseline = baselines.get(name, value)
            half_life = half_lives.get(name, 0.0)
            if half_life <= 0:
                continue
            factor = 0.5 ** (dt_s / half_life)
            values[name] = _clamp(baseline + (value - baseline) * factor)
        return EmotionalState(**values)

    def rounded(self, places: int = 2) -> EmotionalState:
        """A coarser copy. What gets persisted: the mood, not every twitch."""
        return EmotionalState(**{name: round(value, places) for name, value in self.model_dump().items()})

    def describe(self) -> str:
        return ", ".join(f"{name} {value:.2f}" for name, value in sorted(self.model_dump().items()))


class EmotionTuning(BaseModel):
    """Half-lives and nudge magnitudes. Every number the emotion model uses lives here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Seconds for each variable to close half the distance to its baseline.
    half_lives_s: dict[str, float] = Field(
        default_factory=lambda: {
            "valence": 120.0,
            "arousal": 30.0,
            "curiosity": 300.0,
            "boredom": 420.0,
            "social_need": 600.0,
            "confidence": 300.0,
            "energy": 3600.0,
        }
    )
    #: How much faster energy recovers while charging than it decays otherwise.
    charging_energy_half_life_s: float = Field(default=180.0, gt=0)
    #: Signed deltas per stimulus. Small on purpose: a control variable that jumps is a
    #: control variable that oscillates.
    nudges: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            Stimulus.POSITIVE_INTERACTION.value: {"valence": 0.15, "social_need": -0.25, "boredom": -0.6},
            Stimulus.NEGATIVE_INTERACTION.value: {"valence": -0.15, "arousal": 0.1, "boredom": -0.3},
            Stimulus.PRAISE.value: {"valence": 0.25, "confidence": 0.1, "social_need": -0.15, "boredom": -0.5},
            Stimulus.TOUCH.value: {"valence": 0.12, "arousal": 0.15, "social_need": -0.2, "boredom": -0.5},
            Stimulus.PERSON_SEEN.value: {"arousal": 0.1, "boredom": -0.4},
            Stimulus.PERSON_LOST.value: {"valence": -0.05, "social_need": 0.1},
            Stimulus.NOVELTY.value: {"curiosity": 0.2, "arousal": 0.1, "boredom": -0.5},
            Stimulus.CURIOSITY_SATISFIED.value: {"curiosity": -0.25, "valence": 0.05},
            Stimulus.FAILED_MOVEMENT.value: {"confidence": -0.08, "valence": -0.05, "arousal": 0.1},
            Stimulus.SUCCESSFUL_MOVEMENT.value: {"confidence": 0.03},
            Stimulus.OBSTACLE.value: {"arousal": 0.25, "confidence": -0.03},
            Stimulus.LOUD_SOUND.value: {"arousal": 0.3, "boredom": -0.3},
            Stimulus.CHARGING.value: {"energy": 0.05, "arousal": -0.1},
            Stimulus.RESTED.value: {"energy": 0.1, "confidence": 0.05},
        }
    )
    #: How strongly the personality traits pull the baselines. At 0 every robot rests in
    #: the same place; at 1 a sociable robot is permanently hungrier for company.
    trait_influence: float = Field(default=1.0, ge=0.0, le=1.0)
    #: Below this energy, animations that declare themselves energetic are suppressed.
    low_energy_threshold: float = Field(default=0.25, ge=0.0, le=1.0)

    def deltas_for(self, stimulus: Stimulus | str) -> dict[str, float]:
        key = stimulus.value if isinstance(stimulus, Stimulus) else str(stimulus)
        return dict(self.nudges.get(key, {}))


def baselines_for(traits: PersonalityTraits, tuning: EmotionTuning) -> dict[str, float]:
    """Where each variable rests, given a personality.

    The one place traits reach the emotional model. Every entry is a blend between a
    neutral resting point and the trait, weighted by :attr:`EmotionTuning.trait_influence`,
    so turning the influence to zero gives a robot with no personality rather than a robot
    with a broken one.
    """
    weight = tuning.trait_influence

    def blend(neutral: float, trait: float) -> float:
        return _clamp(neutral * (1 - weight) + trait * weight)

    return {
        # A mildly positive resting mood: the robot is not sad by default.
        "valence": blend(0.55, 0.4 + traits.playfulness * 0.3),
        # Calm, a little more so for a patient robot.
        "arousal": blend(0.25, 0.35 - traits.patience * 0.2),
        "curiosity": blend(0.5, traits.curiosity),
        # Boredom rests at "completely bored": with nothing happening it climbs, and every
        # stimulus knocks it back. A patient robot climbs more slowly (see half_life_for).
        "boredom": 1.0,
        "social_need": blend(0.5, traits.sociability),
        # Confidence recovers towards a level set by boldness, never to certainty.
        "confidence": blend(0.7, 0.5 + traits.boldness * 0.4),
        "energy": traits.energy_baseline,
    }


def half_lives_for(traits: PersonalityTraits, tuning: EmotionTuning, *, charging: bool = False) -> dict[str, float]:
    """Per-variable half-lives, adjusted by personality and by whether the robot is charging."""
    half_lives = dict(tuning.half_lives_s)
    # Patience lengthens the boredom climb: a patient robot waits longer before the
    # boredom behaviours become worth doing. It changes the rate, never the ceiling.
    patience_factor = 0.5 + traits.patience * 1.5
    half_lives["boredom"] = half_lives.get("boredom", 420.0) * patience_factor
    if charging:
        half_lives["energy"] = tuning.charging_energy_half_life_s
    return half_lives


class EmotionEngine:
    """Holds one robot's control variables and moves them as time passes and things happen.

    Not an event subscriber by itself: :meth:`record` and :meth:`update` are called by the
    wiring in :mod:`robot.personality.model`, which is what knows about the bus. Keeping
    the maths here and the plumbing there means the decay rules can be tested with three
    lines and no event loop.
    """

    def __init__(
        self,
        traits: PersonalityTraits | None = None,
        *,
        tuning: EmotionTuning | None = None,
        state: EmotionalState | None = None,
        now: float = 0.0,
    ) -> None:
        self.traits = traits or PersonalityTraits()
        self.tuning = tuning or EmotionTuning()
        self._state = state or EmotionalState(
            energy=self.traits.energy_baseline,
            curiosity=self.traits.curiosity,
            social_need=self.traits.sociability,
        )
        self._updated_at = now
        self._charging = False

    @property
    def state(self) -> EmotionalState:
        """The current values. Read after :meth:`update` for the freshest picture."""
        return self._state

    @property
    def charging(self) -> bool:
        return self._charging

    def set_charging(self, charging: bool, now: float | None = None) -> None:
        """Charging changes which half-life energy uses, so it settles the decay first."""
        if now is not None:
            self.update(now)
        self._charging = charging

    def baselines(self) -> dict[str, float]:
        return baselines_for(self.traits, self.tuning)

    def half_lives(self) -> dict[str, float]:
        return half_lives_for(self.traits, self.tuning, charging=self._charging)

    def update(self, now: float) -> EmotionalState:
        """Decay to ``now``. Idempotent for the same instant; never moves time backwards."""
        dt = now - self._updated_at
        if dt <= 0:
            return self._state
        self._updated_at = now
        self._state = self._state.decayed(dt, self.baselines(), self.half_lives())
        return self._state

    def record(self, stimulus: Stimulus | str, *, strength: float = 1.0, now: float | None = None) -> EmotionalState:
        """Apply a stimulus. Decays to ``now`` first so the nudge lands on current values."""
        if now is not None:
            self.update(now)
        deltas = self.tuning.deltas_for(stimulus)
        if not deltas:
            logger.debug("no nudge configured for stimulus %s; ignoring it", stimulus)
            return self._state
        self._state = self._state.nudged(deltas, strength)
        return self._state

    def set_state(self, state: EmotionalState, now: float | None = None) -> None:
        """Install a state wholesale. For restoring a persisted snapshot."""
        self._state = state
        if now is not None:
            self._updated_at = now

    def set_traits(self, traits: PersonalityTraits) -> None:
        """Change the personality. The baselines move; the current values stay where they are
        and drift to the new resting points, which is what makes the change visible rather
        than instantaneous."""
        self.traits = traits

    def __repr__(self) -> str:
        return f"<EmotionEngine {self._state.describe()}>"


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


__all__ = [
    "EmotionEngine",
    "EmotionTuning",
    "EmotionalState",
    "Stimulus",
    "baselines_for",
    "half_lives_for",
]
