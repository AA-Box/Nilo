"""Expressive animation: named, timed sequences the robot performs, defined as data.

    from robot.animation import AnimationEngine, load_library

    engine = AnimationEngine("nilo-sim-01", player, load_library())
    await engine.play("excited_greeting")

An animation is a YAML document: a name, some flags, and steps on five channels (eyes,
head, lift, body, audio) at millisecond offsets. Adding one is adding a file — there is no
Python change and no registration call, which is the whole point
(docs/robot-animation.md).

The engine owns timing, priority, looping, resource ownership, transitions and the energy
gate. It commands the robot through the semantic action vocabulary and nothing else, so
every step an animation performs is still filtered by the safety policy: an animation is a
convenient way to ask, never a way to bypass.

Layering (docs/robot-architecture.md Sect. 7): this package may import ``robot/state`` and
``robot/events``. It must not import ``robot/behavior`` or ``robot/personality`` — they
drive it, not the other way round — and it reaches the action layer only through the
:class:`~robot.animation.engine.AnimationPlayer` protocol, which a caller satisfies.
"""

from robot.animation.engine import (
    DEFAULT_LOW_ENERGY_THRESHOLD,
    AnimationEngine,
    AnimationPlayer,
    AnimationRefused,
    HandlePlayer,
    Playback,
)
from robot.animation.library import (
    BUILTIN_LIBRARY_DIR,
    DEFAULT_LIBRARY_DIR,
    EXPRESSIONS,
    AnimationError,
    AnimationLibrary,
    load_directory,
    load_file,
    load_library,
    parse_document,
)
from robot.animation.model import (
    CHANNEL_ACTIONS,
    CHANNEL_RESOURCES,
    Animation,
    AnimationStep,
    Channel,
)

__all__ = [
    "BUILTIN_LIBRARY_DIR",
    "CHANNEL_ACTIONS",
    "CHANNEL_RESOURCES",
    "DEFAULT_LIBRARY_DIR",
    "DEFAULT_LOW_ENERGY_THRESHOLD",
    "EXPRESSIONS",
    "Animation",
    "AnimationEngine",
    "AnimationError",
    "AnimationLibrary",
    "AnimationPlayer",
    "AnimationRefused",
    "AnimationStep",
    "Channel",
    "HandlePlayer",
    "Playback",
    "load_directory",
    "load_file",
    "load_library",
    "parse_document",
]
