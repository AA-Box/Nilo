"""Personality: the six numbers that make one robot different from another.

Traits are **stable**. They are set once per robot (by a config file, by an owner, by a
factory default) and they do not move on their own — that is the difference between a
trait and the state in :mod:`robot.personality.emotion`, which moves constantly.

All six are normalized 0..1, which makes them comparable, blendable and safe to interpolate.
A trait at 0.5 is "average"; the defaults are all 0.5 except energy, because a robot that
ships asleep looks broken.

    traits = PersonalityTraits(sociability=0.9, curiosity=0.8, energy_baseline=0.7)

What traits may do: bias which behaviour is chosen, and how an animation is performed.
What traits may **not** do: change what safety permits. A maximally bold robot is refused
the same move on the same cliff as a timid one, and a test sweeps every trait across its
full range to prove it (docs/robot-personality.md).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

#: Where :func:`load_traits` looks when given no path, relative to the server directory.
DEFAULT_PERSONALITY_PATH = Path("data") / "robot_personality.yaml"


class PersonalityTraits(BaseModel):
    """Stable dispositions. Normalized 0..1, frozen, and cheap to copy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: How much the robot seeks people out. Raises greeting, following and approaching.
    sociability: float = Field(default=0.5, ge=0.0, le=1.0)
    #: How much novelty is worth. Raises investigating, looking around and exploring.
    curiosity: float = Field(default=0.5, ge=0.0, le=1.0)
    #: How much it embellishes: bigger animations, more optional flourishes.
    playfulness: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Willingness to act with an uncertain outcome. Raises approaching and exploring,
    #: and how fast confidence recovers after something goes wrong. It does **not** widen
    #: a motion limit: boldness is a preference, and limits are not negotiable.
    boldness: float = Field(default=0.5, ge=0.0, le=1.0)
    #: How long it will wait before boredom starts to score. High patience means a longer
    #: fuse, not a different behaviour.
    patience: float = Field(default=0.5, ge=0.0, le=1.0)
    #: The energy level the robot returns to when it is rested and not charging. The
    #: resting point of the energy drive, not a speed limit.
    energy_baseline: float = Field(default=0.8, ge=0.0, le=1.0)

    def blend(self, other: PersonalityTraits, weight: float = 0.5) -> PersonalityTraits:
        """Interpolate towards another personality. For presets and for gradual change."""
        weight = min(1.0, max(0.0, weight))
        values = {
            name: getattr(self, name) * (1 - weight) + getattr(other, name) * weight
            for name in type(self).model_fields
        }
        return PersonalityTraits(**values)

    def with_overrides(self, **overrides: float) -> PersonalityTraits:
        return PersonalityTraits(**{**self.model_dump(), **overrides})

    def describe(self) -> str:
        """One line, for a log or a CLI. Names the traits that are away from average."""
        strong = [
            f"{name} {value:.2f}"
            for name, value in sorted(self.model_dump().items())
            if abs(value - 0.5) >= 0.2
        ]
        return ", ".join(strong) if strong else "unremarkable in every direction"


#: Ready-made personalities. Presets rather than a wizard: three named points in the space
#: cover most of what an owner actually wants, and the rest is a YAML file.
PRESETS: dict[str, PersonalityTraits] = {
    "balanced": PersonalityTraits(),
    "puppy": PersonalityTraits(
        sociability=0.95, curiosity=0.8, playfulness=0.9, boldness=0.7, patience=0.2, energy_baseline=0.9
    ),
    "cat": PersonalityTraits(
        sociability=0.25, curiosity=0.85, playfulness=0.5, boldness=0.4, patience=0.8, energy_baseline=0.5
    ),
    "assistant": PersonalityTraits(
        sociability=0.7, curiosity=0.4, playfulness=0.2, boldness=0.5, patience=0.9, energy_baseline=0.7
    ),
}


def get_preset(name: str) -> PersonalityTraits:
    try:
        return PRESETS[name]
    except KeyError:
        raise KeyError(f"unknown personality preset {name!r}; known: {', '.join(sorted(PRESETS))}") from None


def traits_from_mapping(data: Any) -> PersonalityTraits:
    """Build traits from a parsed document. A ``preset`` key seeds them; the rest override.

    ```yaml
    personality:
      preset: puppy
      patience: 0.6      # a puppy that can sit still
    ```
    """
    if data is None:
        return PersonalityTraits()
    if not isinstance(data, dict):
        raise ValueError("the personality document must be a mapping")
    block = data.get("personality", data)
    if not isinstance(block, dict):
        raise ValueError("the 'personality' key must hold a mapping")
    values = {str(key): value for key, value in block.items()}
    preset = values.pop("preset", None)
    base = get_preset(str(preset)) if preset is not None else PersonalityTraits()
    return base.with_overrides(**{key: float(value) for key, value in values.items()})


def load_traits(path: str | Path | None = None) -> PersonalityTraits:
    """Read a personality file, or return the balanced default when there is none."""
    location = Path(path) if path is not None else DEFAULT_PERSONALITY_PATH
    if not location.exists():
        logger.debug("no personality file at %s; using the balanced default", location)
        return PersonalityTraits()
    import yaml

    with location.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    traits = traits_from_mapping(document)
    logger.info("robot personality loaded from %s: %s", location, traits.describe())
    return traits


__all__ = [
    "DEFAULT_PERSONALITY_PATH",
    "PRESETS",
    "PersonalityTraits",
    "get_preset",
    "load_traits",
    "traits_from_mapping",
]
