"""Deterministic autonomy: what the robot does when nobody is telling it to.

The engine is a utility/score-based scheduler, not an LLM. A model is never asked what to
do next, never asked to arbitrate, and never in the loop at all — the robot behaves
identically with every LLM provider hard-failing, which is a requirement and not a
fallback (docs/robot-architecture.md Sect. 2.6).

    from robot.behavior import AutonomyMode, BehaviorEngine

    engine = BehaviorEngine("nilo-sim-01", runtime.robot("nilo-sim-01"), runtime.world)
    engine.start()
    print(engine.explain())

The pieces:

``tuning``     every number the engine compares against, loadable from YAML
``base``       what a behaviour is: can_run / score / execute / cancel, and the context
``scheduler``  filtering, scoring, arbitration, preemption and the debug decision record
``builtins``   the sixteen behaviours the robot ships with
``engine``     the assembly: a scheduler bound to a world source and a robot handle

Layering (docs/robot-architecture.md Sect. 7): this package may import ``robot/actions``,
``robot/state``, ``robot/personality`` and ``robot/events``. It must never import
``robot/devices`` or ``robot/simulator``. Behaviours *propose*; the action layer disposes,
and the safety policy underneath it cannot be reached from here — a behaviour with a
score of 1.0 still gets its move rejected on a cliff.
"""

from robot.behavior.base import (
    EXPRESSIVE_RESOURCES,
    AutonomyMode,
    Behavior,
    BehaviorCategory,
    BehaviorContext,
    BehaviorMemory,
    BehaviorOutcome,
    BehaviorPriority,
    BehaviorResult,
    Drives,
    NeutralDrives,
    RobotCommands,
)
from robot.behavior.builtins import (
    BUILTIN_BEHAVIORS,
    ApproachPersonBehavior,
    BoredBehavior,
    ChargingBehavior,
    ExploreBehavior,
    FollowPersonBehavior,
    GoToChargerBehavior,
    GreetPersonBehavior,
    IdleBehavior,
    InvestigateObjectBehavior,
    LookAroundBehavior,
    LookAtPersonBehavior,
    LowBatteryBehavior,
    ReactToSoundBehavior,
    ReactToTouchBehavior,
    SleepBehavior,
    WakeBehavior,
    default_behaviors,
)
from robot.behavior.engine import BehaviorEngine, StaticWorld, WorldSource
from robot.behavior.scheduler import (
    BehaviorCandidate,
    BehaviorDecision,
    BehaviorRegistry,
    BehaviorScheduler,
)
from robot.behavior.tuning import BehaviorTuning, load_tuning, tuning_from_mapping

__all__ = [
    "BUILTIN_BEHAVIORS",
    "EXPRESSIVE_RESOURCES",
    "ApproachPersonBehavior",
    "AutonomyMode",
    "Behavior",
    "BehaviorCandidate",
    "BehaviorCategory",
    "BehaviorContext",
    "BehaviorDecision",
    "BehaviorEngine",
    "BehaviorMemory",
    "BehaviorOutcome",
    "BehaviorPriority",
    "BehaviorRegistry",
    "BehaviorResult",
    "BehaviorScheduler",
    "BehaviorTuning",
    "BoredBehavior",
    "ChargingBehavior",
    "Drives",
    "ExploreBehavior",
    "FollowPersonBehavior",
    "GoToChargerBehavior",
    "GreetPersonBehavior",
    "IdleBehavior",
    "InvestigateObjectBehavior",
    "LookAroundBehavior",
    "LookAtPersonBehavior",
    "LowBatteryBehavior",
    "NeutralDrives",
    "ReactToSoundBehavior",
    "ReactToTouchBehavior",
    "RobotCommands",
    "SleepBehavior",
    "StaticWorld",
    "WakeBehavior",
    "WorldSource",
    "default_behaviors",
    "load_tuning",
    "tuning_from_mapping",
]
