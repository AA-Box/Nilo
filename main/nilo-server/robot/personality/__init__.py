"""Personality and the internal control variables that make a robot feel consistent.

    from robot.personality import PersonalityModel, load_traits

    personality = PersonalityModel("nilo-sim-01", traits=load_traits())
    personality.attach(runtime.events)
    engine.set_drives(personality)

Two halves with two lifetimes. **Traits** (``traits.py``) are stable — six normalized
dispositions that define one robot against another, set from a file or a preset and
persisted. **Control variables** (``emotion.py``) move constantly: seven numbers that
decay towards trait-derived baselines and are nudged by what happens.

They are **not emotions**, and nothing in this package says they are. They are inputs to a
utility function, named after what they bias. A robot that claims to feel something it
does not is lying to its owner, and this package is written to avoid that
(docs/robot-personality.md).

Layering: personality may import ``robot/state`` and ``robot/events``. It may not import
``robot/actions``, ``robot/behavior`` or ``robot/safety``. It influences which action is
proposed; it cannot influence whether one is allowed.
"""

from robot.personality.emotion import (
    EmotionalState,
    EmotionEngine,
    EmotionTuning,
    Stimulus,
    baselines_for,
    half_lives_for,
)
from robot.personality.model import (
    DEFAULT_BEHAVIOR_STIMULI,
    PersonalityModel,
    drives_snapshot,
)
from robot.personality.store import (
    DEFAULT_STORE_DIR,
    PersonalityRecord,
    PersonalityStore,
)
from robot.personality.traits import (
    DEFAULT_PERSONALITY_PATH,
    PRESETS,
    PersonalityTraits,
    get_preset,
    load_traits,
    traits_from_mapping,
)

__all__ = [
    "DEFAULT_BEHAVIOR_STIMULI",
    "DEFAULT_PERSONALITY_PATH",
    "DEFAULT_STORE_DIR",
    "PRESETS",
    "EmotionEngine",
    "EmotionTuning",
    "EmotionalState",
    "PersonalityModel",
    "PersonalityRecord",
    "PersonalityStore",
    "PersonalityTraits",
    "Stimulus",
    "baselines_for",
    "drives_snapshot",
    "get_preset",
    "half_lives_for",
    "load_traits",
    "traits_from_mapping",
]
