"""Animations as data: what one is, and what it is allowed to say.

An animation is a name, some flags, and a list of timed steps on named channels. It is
**not** code. Adding one is adding a YAML file; there is no Python change, no import to
register, and no place to hide a hardcoded ``await asyncio.sleep(0.4)``
(docs/robot-animation.md).

```yaml
name: excited_greeting
description: Eyes light up, a small head bounce, a short wiggle.
priority: 60
energetic: true
transition: neutral        # what the face is left showing afterwards
steps:
  - {at_ms:   0, channel: eyes, action: expression, args: {emotion: excited, intensity_pct: 90}}
  - {at_ms:   0, channel: head, action: angle,      args: {pitch_deg: 12}}
  - {at_ms: 250, channel: head, action: angle,      args: {pitch_deg: -6}}
  - {at_ms: 500, channel: body, action: turn,       args: {angle_deg: 12}}
  - {at_ms: 700, channel: body, action: turn,       args: {angle_deg: -12}}
  - {at_ms: 200, channel: audio, action: cue,       args: {sound: chirp}}
```

The channel vocabulary is closed and maps onto the resource ledger the action queue
already uses, so an animation that moves the head cannot run while something else owns the
head. Steps are sorted by ``at_ms`` on load, and ``at_ms`` is an offset from the start of
the animation rather than a delay from the previous step — an author edits one step
without shifting everything after it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from robot.state.actions import Resource


class Channel(str, Enum):
    """What part of the robot a step commands. Closed, and mapped onto resources."""

    EYES = "eyes"
    HEAD = "head"
    LIFT = "lift"
    BODY = "body"
    AUDIO = "audio"


#: Which subsystem each channel claims while an animation plays. The same ledger the
#: action queue uses (``robot/actions/queue.py``), so an animation and an action cannot
#: both own the head.
CHANNEL_RESOURCES: dict[Channel, Resource] = {
    Channel.EYES: Resource.DISPLAY,
    Channel.HEAD: Resource.HEAD,
    Channel.LIFT: Resource.LIFT,
    Channel.BODY: Resource.DRIVE,
    Channel.AUDIO: Resource.AUDIO,
}

#: The actions each channel understands. An animation naming anything else fails to load,
#: loudly, rather than silently doing nothing on a robot at three in the morning.
CHANNEL_ACTIONS: dict[Channel, frozenset[str]] = {
    Channel.EYES: frozenset({"expression", "look_at"}),
    Channel.HEAD: frozenset({"angle", "look_at"}),
    Channel.LIFT: frozenset({"height"}),
    Channel.BODY: frozenset({"turn", "move"}),
    Channel.AUDIO: frozenset({"cue"}),
}


class AnimationStep(BaseModel):
    """One command, on one channel, at one offset from the start."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at_ms: int = Field(default=0, ge=0)
    channel: Channel
    action: str
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_action(self) -> AnimationStep:
        allowed = CHANNEL_ACTIONS[self.channel]
        if self.action not in allowed:
            raise ValueError(
                f"channel {self.channel.value!r} has no action {self.action!r}; "
                f"known: {', '.join(sorted(allowed))}"
            )
        return self

    @property
    def resource(self) -> Resource:
        return CHANNEL_RESOURCES[self.channel]

    def describe(self) -> str:
        return f"{self.at_ms}ms {self.channel.value}.{self.action}"


class Animation(BaseModel):
    """A named, timed sequence. Frozen, so a library entry cannot be edited by a player."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    #: Higher wins when two animations want the same channel. Deliberately the same scale
    #: as the behaviour bands, so "this animation outranks that one" reads the same way.
    priority: int = Field(default=50, ge=0, le=100)
    #: Whether it repeats until something cancels it. A looping animation must be cheap.
    loop: bool = False
    #: Whether it is big enough that a tired robot should skip it. The energy gate reads
    #: this, and nothing else does.
    energetic: bool = False
    #: The expression the face is left in when the animation ends or is cancelled. This is
    #: the transition: without it a cancelled animation leaves the eyes mid-blink.
    transition: str | None = None
    #: Free-form labels, for a behaviour that wants "any greeting animation".
    tags: tuple[str, ...] = ()
    steps: tuple[AnimationStep, ...] = ()
    #: How long the whole thing lasts. Defaults to the last step's offset; set it longer to
    #: hold the final pose.
    duration_ms: int | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _name_is_an_identifier(cls, value: str) -> str:
        if not value or not value.replace("_", "").isalnum():
            raise ValueError(f"animation names are snake_case identifiers, not {value!r}")
        return value

    @model_validator(mode="after")
    def _sorted_and_bounded(self) -> Animation:
        if self.steps != tuple(sorted(self.steps, key=lambda step: step.at_ms)):
            object.__setattr__(self, "steps", tuple(sorted(self.steps, key=lambda step: step.at_ms)))
        if self.loop and not self.steps:
            raise ValueError(f"animation {self.name!r} loops but has no steps")
        return self

    @property
    def length_ms(self) -> int:
        """How long a single pass takes, including any hold at the end."""
        last = self.steps[-1].at_ms if self.steps else 0
        return max(last, self.duration_ms or 0)

    @property
    def channels(self) -> frozenset[Channel]:
        return frozenset(step.channel for step in self.steps)

    @property
    def required_resources(self) -> frozenset[Resource]:
        """Everything this animation owns while it plays."""
        return frozenset(CHANNEL_RESOURCES[channel] for channel in self.channels)

    def conflicts_with(self, other: Animation) -> bool:
        return bool(self.required_resources & other.required_resources)

    def describe(self) -> str:
        return (
            f"{self.name} ({len(self.steps)} steps, {self.length_ms}ms, "
            f"priority {self.priority}{', loops' if self.loop else ''})"
        )


__all__ = [
    "CHANNEL_ACTIONS",
    "CHANNEL_RESOURCES",
    "Animation",
    "AnimationStep",
    "Channel",
]
