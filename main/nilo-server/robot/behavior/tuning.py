"""Every number the behaviour engine compares against, in one place.

The rule this file exists to enforce: **no numeric constant is written inside a behaviour**
(docs/robot-behavior.md). A magic ``0.8`` buried in a scoring function is a value nobody
can find, nobody can change without a deploy, and nobody can tell apart from a typo. Each
one here has a name, a default, a unit in that name, and a comment saying what moving it
does.

Loaded like the safety limits (``robot/safety/limits.py``) and for the same reasons: from
its own file rather than from the server config dict, which is replaced wholesale in
manager-api mode, and constructed per runtime rather than as a module singleton so two
robots can be tuned differently in one process.

Nothing here can weaken safety. These numbers decide *which* action is proposed; whether
it is allowed is decided one layer down, by a policy that cannot see this file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

#: Where :func:`load_tuning` looks when given no path, relative to the server directory.
DEFAULT_TUNING_PATH = Path("data") / "robot_behavior.yaml"


class BehaviorTuning(BaseModel):
    """Scoring weights, thresholds and timings for the built-in behaviours.

    Scores are utilities in 0..1. They are not probabilities and they do not have to sum
    to anything: the scheduler ranks them, it does not normalize them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # -- scheduler ---------------------------------------------------------------------------
    #: How often the scheduler re-evaluates when it owns its own loop.
    tick_interval_s: float = Field(default=0.5, gt=0)
    #: A candidate below this never runs, even when it is the only one. The floor exists so
    #: "nothing is worth doing" is expressible; :class:`IdleBehavior` sits just above it.
    min_score: float = Field(default=0.01, ge=0.0, le=1.0)
    #: How much better a challenger must be before it preempts a running behaviour of the
    #: same priority band. Without a margin, two near-equal candidates thrash.
    preemption_margin: float = Field(default=0.15, ge=0.0, le=1.0)
    #: Amplitude of the seeded random jitter added to every score, so a robot with two
    #: equally good options does not always pick the same one. Zero makes runs replayable
    #: without relying on the seed.
    score_jitter: float = Field(default=0.03, ge=0.0, le=0.5)

    # -- freshness -----------------------------------------------------------------------------
    #: How recently a person must have been seen to count as present.
    person_fresh_s: float = Field(default=5.0, gt=0)
    #: How recently an object must have been seen to be worth investigating.
    object_fresh_s: float = Field(default=8.0, gt=0)
    #: A touch or a sound older than this is not a reaction trigger any more.
    stimulus_fresh_s: float = Field(default=3.0, gt=0)

    # -- idle / boredom -------------------------------------------------------------------------
    #: The score of doing nothing. Everything interesting has to beat this.
    idle_score: float = Field(default=0.05, ge=0.0, le=1.0)
    #: Seconds of no interaction before boredom starts to score at all.
    boredom_onset_s: float = Field(default=45.0, gt=0)
    #: Seconds of no interaction at which boredom reaches :attr:`boredom_max_score`.
    boredom_full_s: float = Field(default=300.0, gt=0)
    boredom_max_score: float = Field(default=0.55, ge=0.0, le=1.0)
    #: Seconds between two boredom expressions, so it is a mood and not a tic.
    boredom_cooldown_s: float = Field(default=60.0, ge=0)

    # -- look around / explore --------------------------------------------------------------------
    look_around_base_score: float = Field(default=0.18, ge=0.0, le=1.0)
    look_around_cooldown_s: float = Field(default=20.0, ge=0)
    #: How much curiosity adds to looking around and exploring at full strength.
    curiosity_weight: float = Field(default=0.35, ge=0.0, le=1.0)
    #: Curiosity's contribution to looking around. Smaller than :attr:`curiosity_weight`,
    #: which is exploration's: sweeping the head learns less than driving somewhere.
    look_around_curiosity_weight: float = Field(default=0.18, ge=0.0, le=1.0)
    #: Boredom's contribution to looking around.
    look_around_boredom_weight: float = Field(default=0.10, ge=0.0, le=1.0)
    #: How much looking around is worth while somebody is in the room. Below 1, because a
    #: person is more interesting than the wallpaper.
    look_around_busy_scale: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Boredom's contribution to exploring.
    explore_boredom_weight: float = Field(default=0.15, ge=0.0, le=1.0)
    explore_base_score: float = Field(default=0.22, ge=0.0, le=1.0)
    explore_cooldown_s: float = Field(default=30.0, ge=0)
    #: One exploration leg. Kept short so the robot re-evaluates the world often.
    explore_distance_mm: int = Field(default=300, ge=0)
    explore_turn_deg: int = Field(default=45, ge=0)

    # -- social ----------------------------------------------------------------------------------
    greet_score: float = Field(default=0.80, ge=0.0, le=1.0)
    #: A familiar face is worth greeting more than a stranger.
    greet_known_bonus: float = Field(default=0.10, ge=0.0, le=1.0)
    #: Do not greet the same person again inside this window. The "recently greeted =
    #: near zero" rule from the design.
    greet_cooldown_s: float = Field(default=120.0, ge=0)
    #: How much sociability scales the social behaviours at full strength.
    sociability_weight: float = Field(default=0.25, ge=0.0, le=1.0)
    look_at_person_score: float = Field(default=0.45, ge=0.0, le=1.0)
    #: Below this normalized off-centre error the head is already pointed well enough.
    look_at_deadband: float = Field(default=0.08, ge=0.0, le=0.5)
    approach_score: float = Field(default=0.40, ge=0.0, le=1.0)
    #: Do not approach a person who is already this close.
    approach_min_distance_mm: int = Field(default=900, ge=0)
    #: One approach leg, re-evaluated on the next tick.
    approach_step_mm: int = Field(default=300, ge=0)
    approach_cooldown_s: float = Field(default=10.0, ge=0)
    follow_score: float = Field(default=0.50, ge=0.0, le=1.0)
    follow_duration_ms: int = Field(default=4000, ge=0)
    follow_stop_distance_mm: int = Field(default=600, ge=0)

    # -- reactions ---------------------------------------------------------------------------------
    #: Reactions outrank most things: a robot that ignores being touched feels broken.
    touch_score: float = Field(default=0.85, ge=0.0, le=1.0)
    touch_cooldown_s: float = Field(default=4.0, ge=0)
    #: How much a high social need adds to reacting to a touch. Small: being touched is
    #: worth reacting to whether or not the robot wanted company.
    touch_social_weight: float = Field(default=0.05, ge=0.0, le=1.0)
    sound_score: float = Field(default=0.60, ge=0.0, le=1.0)
    #: How much arousal adds to reacting to a sound.
    sound_arousal_weight: float = Field(default=0.10, ge=0.0, le=1.0)
    #: Ambient level above which a sound counts as something to react to.
    sound_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    sound_cooldown_s: float = Field(default=8.0, ge=0)
    investigate_score: float = Field(default=0.35, ge=0.0, le=1.0)
    investigate_cooldown_s: float = Field(default=25.0, ge=0)

    # -- power ---------------------------------------------------------------------------------------
    #: Battery percentage below which docking outranks everything social.
    battery_low_percent: int = Field(default=20, ge=0, le=100)
    #: Battery percentage below which docking is the only thing worth doing.
    battery_critical_percent: int = Field(default=10, ge=0, le=100)
    #: Score of a low-battery warning while above the critical line.
    low_battery_score: float = Field(default=0.55, ge=0.0, le=1.0)
    #: Score of docking at exactly the low threshold. It rises to
    #: :attr:`go_to_charger_critical_score` as the battery falls to critical.
    go_to_charger_score: float = Field(default=0.70, ge=0.0, le=1.0)
    go_to_charger_critical_score: float = Field(default=0.99, ge=0.0, le=1.0)
    go_to_charger_step_mm: int = Field(default=300, ge=0)
    charging_score: float = Field(default=0.90, ge=0.0, le=1.0)
    #: Battery percentage at which a charging robot is willing to do something else.
    charged_enough_percent: int = Field(default=95, ge=0, le=100)

    # -- sleep / wake ------------------------------------------------------------------------------------
    #: Seconds of no interaction and no stimulus before the robot goes to sleep.
    sleep_after_idle_s: float = Field(default=600.0, gt=0)
    sleep_score: float = Field(default=0.65, ge=0.0, le=1.0)
    #: Score of waking up when something happens while asleep. Above sleep, so it wins.
    wake_score: float = Field(default=0.95, ge=0.0, le=1.0)

    # -- runtime bounds -------------------------------------------------------------------------------------
    #: Default floor on how long a behaviour runs before anything may preempt it. Stops
    #: a greeting being cut off half a syllable in.
    default_min_runtime_s: float = Field(default=0.5, ge=0)
    #: Default ceiling. A behaviour that overruns is cancelled, which is how a stuck
    #: execute() becomes a log line instead of a robot that never does anything else.
    default_max_runtime_s: float = Field(default=30.0, gt=0)

    def with_overrides(self, **overrides: Any) -> BehaviorTuning:
        """A copy with fields replaced. Validated, so a bad override raises here."""
        return BehaviorTuning(**{**self.model_dump(), **overrides})


def tuning_from_mapping(data: Any) -> BehaviorTuning:
    """Build tuning from a parsed document. Unknown keys raise rather than being ignored."""
    if data is None:
        return BehaviorTuning()
    if not isinstance(data, dict):
        raise ValueError("the behaviour tuning document must be a mapping")
    block = data.get("behavior", data)
    if not isinstance(block, dict):
        raise ValueError("the 'behavior' key must hold a mapping")
    return BehaviorTuning(**{str(key): value for key, value in block.items()})


def load_tuning(path: str | Path | None = None) -> BehaviorTuning:
    """Read a tuning file, or return the defaults when there is none.

    A file that exists but cannot be parsed raises: an operator who wrote a tuning file
    and got the built-in defaults would have no way to notice.
    """
    location = Path(path) if path is not None else DEFAULT_TUNING_PATH
    if not location.exists():
        logger.debug("no behaviour tuning file at %s; using the built-in defaults", location)
        return BehaviorTuning()
    import yaml

    with location.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    tuning = tuning_from_mapping(document)
    logger.info("robot behaviour tuning loaded from %s", location)
    return tuning


__all__ = ["DEFAULT_TUNING_PATH", "BehaviorTuning", "load_tuning", "tuning_from_mapping"]
