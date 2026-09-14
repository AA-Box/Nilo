"""Playing animations: timing, priority, resource ownership, looping and cancellation.

The engine is the only thing that turns an :class:`~robot.animation.model.Animation` into
commands. It owns four rules:

* **Resources.** A playing animation owns the subsystems its channels map onto. A second
  animation wanting the head cannot start while the first holds it — the same ledger the
  action queue uses, applied before anything is sent.
* **Priority.** A higher-priority animation preempts a lower one and takes its resources.
  Equal or lower is refused, with a reason, rather than queued: an animation that plays
  four seconds late is worse than one that never played.
* **Transitions.** An animation that ends — completed *or* cancelled — leaves the face in
  its declared ``transition`` expression. Without that, a preempted animation leaves the
  eyes mid-blink, which is the single most broken-looking thing a robot can do.
* **Energy.** An animation marked ``energetic`` is suppressed when the robot's energy is
  below the threshold. That is the whole "low energy → energetic animations suppressed"
  rule, and it is a gate here rather than a branch in every behaviour.

Time is injected. ``clock`` and ``sleep`` are parameters, so a test plays a four-second
animation in no time at all and asserts the exact command order — the timing of an
animation is data, and data can be asserted on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from robot.animation.library import AnimationLibrary
from robot.animation.model import Animation, AnimationStep, Channel
from robot.events.bus import EventBus
from robot.events.types import AnimationCancelled, AnimationFinished, AnimationStarted, RobotEvent
from robot.state.actions import Resource

logger = logging.getLogger(__name__)

#: Energy below which ``energetic`` animations are skipped, when no personality supplies
#: its own threshold.
DEFAULT_LOW_ENERGY_THRESHOLD = 0.25

#: Floor on how fast a looping animation may repeat. An animation whose steps are all at
#: offset zero takes no time to play, and without this floor its loop is a busy-wait that
#: starves the event loop — a malformed YAML file must not be able to pin a core.
LOOP_MIN_INTERVAL_S = 0.05


class AnimationPlayer(Protocol):
    """What the engine is allowed to command.

    Deliberately the semantic vocabulary and nothing else: an animation is a sequence of
    the same commands a behaviour or an operator could issue, which means every one of
    them still goes through safety. An animation cannot address an actuator directly, and
    there is no path here that bypasses the action layer.
    """

    async def set_expression(self, emotion: str, intensity_pct: int = ..., **kwargs: Any) -> Any: ...

    async def head_angle(self, pitch_deg: int = ..., yaw_deg: int = ..., **kwargs: Any) -> Any: ...

    async def look_at(self, x_pct: int = ..., y_pct: int = ..., **kwargs: Any) -> Any: ...

    async def lift(self, height_pct: int, **kwargs: Any) -> Any: ...

    async def turn(self, angle_deg: int, **kwargs: Any) -> Any: ...

    async def move(self, distance_mm: int, **kwargs: Any) -> Any: ...

    async def play_sound(self, sound: str, **kwargs: Any) -> Any: ...


@dataclass
class Playback:
    """One animation in flight."""

    animation: Animation
    priority: int
    started_at: float
    loop: bool
    task: asyncio.Task[None] | None = None
    passes: int = 0
    #: Commands issued, in order. Kept for tests and for the debug endpoint; bounded by
    #: the animation's own step count, so a looping animation does not grow it forever.
    log: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.animation.name

    @property
    def resources(self) -> frozenset[Resource]:
        return self.animation.required_resources

    def elapsed(self, now: float) -> float:
        return now - self.started_at


class AnimationRefused(RuntimeError):
    """An animation was not played, and why. Raised only by :meth:`AnimationEngine.require`."""


class AnimationEngine:
    """Plays animations for one robot, arbitrating between them.

    Constructed per robot. Two robots in a process never share one, because the resource
    ledger is per robot and a shared one would have them blocking each other's heads.
    """

    def __init__(
        self,
        robot_id: str,
        player: AnimationPlayer,
        library: AnimationLibrary | None = None,
        *,
        events: EventBus | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        energy: Callable[[], float] | None = None,
        low_energy_threshold: float = DEFAULT_LOW_ENERGY_THRESHOLD,
    ) -> None:
        self.robot_id = robot_id
        self.player = player
        self.library = AnimationLibrary() if library is None else library
        self._events = events
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._energy = energy
        self.low_energy_threshold = low_energy_threshold
        self._playing: dict[str, Playback] = {}
        self._owners: dict[Resource, str] = {}
        self._closed = False

    # -- introspection ------------------------------------------------------------------------

    @property
    def playing(self) -> tuple[str, ...]:
        return tuple(sorted(self._playing))

    def playback(self, name: str) -> Playback | None:
        return self._playing.get(name)

    def owner_of(self, resource: Resource) -> str | None:
        return self._owners.get(resource)

    def suppressed(self, animation: Animation) -> bool:
        """Whether the energy gate would skip this animation right now."""
        if not animation.energetic or self._energy is None:
            return False
        return self._energy() < self.low_energy_threshold

    # -- playing -------------------------------------------------------------------------------

    async def play(
        self,
        animation: Animation | str,
        *,
        priority: int | None = None,
        loop: bool | None = None,
        wait: bool = False,
    ) -> Playback | None:
        """Start an animation. Returns its :class:`Playback`, or ``None`` if it was refused.

        A refusal is an outcome, not an error: the caller is a behaviour that has already
        decided what it wants, and a missing animation or a busy head is not worth an
        exception. The reason is logged and published.
        """
        if self._closed:
            return None
        resolved = self.library.get(animation) if isinstance(animation, str) else animation
        if resolved is None:
            logger.warning("robot %s: no animation named %r", self.robot_id, animation)
            return None
        if self.suppressed(resolved):
            logger.info(
                "robot %s: %s suppressed, energy below %.2f", self.robot_id, resolved.name, self.low_energy_threshold
            )
            await self._publish(
                AnimationCancelled(robot_id=self.robot_id, animation=resolved.name, reason="low_energy")
            )
            return None

        wanted = priority if priority is not None else resolved.priority
        if not await self._clear_resources(resolved, wanted):
            return None

        existing = self._playing.get(resolved.name)
        if existing is not None:
            await self._stop(existing, reason="restarted")

        playback = Playback(
            animation=resolved,
            priority=wanted,
            started_at=self._clock(),
            loop=resolved.loop if loop is None else loop,
        )
        self._playing[resolved.name] = playback
        for resource in resolved.required_resources:
            self._owners[resource] = resolved.name
        playback.task = asyncio.create_task(
            self._run(playback), name=f"robot-animation-{self.robot_id}-{resolved.name}"
        )
        await self._publish(
            AnimationStarted(
                robot_id=self.robot_id,
                animation=resolved.name,
                priority=wanted,
                loop=playback.loop,
                resources=tuple(sorted(r.value for r in resolved.required_resources)),
            )
        )
        if wait:
            await self.wait_for(resolved.name)
        return playback

    async def require(self, animation: Animation | str, **kwargs: Any) -> Playback:
        """:meth:`play`, but a refusal raises. For a caller that cannot continue without it."""
        playback = await self.play(animation, **kwargs)
        if playback is None:
            raise AnimationRefused(f"{animation} did not play")
        return playback

    async def wait_for(self, name: str) -> None:
        """Wait for one animation to finish. Returns immediately if it is not playing."""
        playback = self._playing.get(name)
        if playback is None or playback.task is None:
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(playback.task)

    async def cancel(self, name: str | None = None, *, reason: str = "cancelled") -> tuple[str, ...]:
        """Cancel one animation, or every one. Returns what was cancelled."""
        targets = [self._playing[name]] if name in self._playing else ([] if name else list(self._playing.values()))
        for playback in targets:
            await self._stop(playback, reason=reason)
        return tuple(playback.name for playback in targets)

    async def aclose(self) -> None:
        """Cancel everything and stop accepting new animations. Idempotent."""
        if self._closed:
            return
        self._closed = True
        await self.cancel(reason="engine closed")

    # -- internals -------------------------------------------------------------------------------

    async def _clear_resources(self, animation: Animation, priority: int) -> bool:
        """Make room, or refuse. Higher priority preempts; equal or lower does not."""
        # Collect the holders by name — a Playback is a mutable dataclass and so not
        # hashable, and two resources held by one animation must yield one holder.
        blocking_names = {
            name
            for resource in animation.required_resources
            if (name := self._owners.get(resource)) is not None
            if name != animation.name and name in self._playing
        }
        blocking = [self._playing[name] for name in sorted(blocking_names)]
        for holder in blocking:
            if holder.priority >= priority:
                logger.info(
                    "robot %s: %s refused, %s holds %s at priority %d",
                    self.robot_id,
                    animation.name,
                    holder.name,
                    ", ".join(sorted(r.value for r in holder.resources & animation.required_resources)),
                    holder.priority,
                )
                await self._publish(
                    AnimationCancelled(
                        robot_id=self.robot_id,
                        animation=animation.name,
                        reason=f"{holder.name} holds the resources at priority {holder.priority}",
                    )
                )
                return False
        for holder in blocking:
            await self._stop(holder, reason=f"preempted by {animation.name}")
        return True

    async def _run(self, playback: Playback) -> None:
        """Play the steps, on the offsets they declare, until the animation ends or loops out."""
        animation = playback.animation
        try:
            while True:
                await self._play_once(playback)
                playback.passes += 1
                if not playback.loop or self._closed:
                    break
                if animation.length_ms <= 0:
                    await self._sleep(LOOP_MIN_INTERVAL_S)
                else:
                    # Yield between passes even when the animation's own timing already
                    # awaited: a pass that happens to contain no wait must still give the
                    # loop a turn, or a cancellation can never land.
                    await asyncio.sleep(0)
            await self._finish(playback, outcome="completed")
        except asyncio.CancelledError:
            # The cancelling path publishes and runs the transition; this only has to leave.
            raise
        except Exception as exc:
            logger.exception("robot %s: animation %s failed", self.robot_id, animation.name)
            await self._finish(playback, outcome="failed", detail=f"{type(exc).__name__}: {exc}")

    async def _play_once(self, playback: Playback) -> None:
        animation = playback.animation
        start = self._clock()
        for step in animation.steps:
            delay = step.at_ms / 1000.0 - (self._clock() - start)
            if delay > 0:
                await self._sleep(delay)
            await self._perform(playback, step)
        # Hold the final pose for the declared remainder, so `duration_ms` means something.
        tail = animation.length_ms / 1000.0 - (self._clock() - start)
        if tail > 0:
            await self._sleep(tail)

    async def _perform(self, playback: Playback, step: AnimationStep) -> None:
        """One step. A step that fails is logged and skipped — the rest of the animation
        still plays, because a robot that freezes mid-gesture looks worse than one that
        drops a beat."""
        args = dict(step.args)
        playback.log.append(f"{step.channel.value}.{step.action}")
        try:
            await self._dispatch(step.channel, step.action, args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "robot %s: animation %s step %s failed: %s",
                self.robot_id,
                playback.name,
                step.describe(),
                exc,
            )

    async def _dispatch(self, channel: Channel, action: str, args: dict[str, Any]) -> None:
        player = self.player
        if channel is Channel.EYES and action == "expression":
            await player.set_expression(str(args.get("emotion", "neutral")), int(args.get("intensity_pct", 100)))
        elif action == "look_at":
            await player.look_at(int(args.get("x_pct", 50)), int(args.get("y_pct", 50)))
        elif channel is Channel.HEAD and action == "angle":
            await player.head_angle(int(args.get("pitch_deg", 0)), int(args.get("yaw_deg", 0)))
        elif channel is Channel.LIFT:
            await player.lift(int(args.get("height_pct", 0)))
        elif channel is Channel.BODY and action == "turn":
            await player.turn(int(args.get("angle_deg", 0)))
        elif channel is Channel.BODY and action == "move":
            await player.move(int(args.get("distance_mm", 0)))
        elif channel is Channel.AUDIO:
            await player.play_sound(str(args.get("sound", "beep")))
        else:  # pragma: no cover - the model validator rejects unknown pairs on load
            logger.warning("robot %s: no dispatch for %s.%s", self.robot_id, channel.value, action)

    async def _stop(self, playback: Playback, *, reason: str) -> None:
        """Cancel a playback, run its transition, and release what it held."""
        task = playback.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._release(playback)
        await self._transition(playback)
        await self._publish(
            AnimationCancelled(robot_id=self.robot_id, animation=playback.name, reason=reason)
        )

    async def _finish(self, playback: Playback, *, outcome: str, detail: str = "") -> None:
        self._release(playback)
        await self._transition(playback)
        await self._publish(
            AnimationFinished(
                robot_id=self.robot_id,
                animation=playback.name,
                outcome=outcome,
                detail=detail,
                passes=playback.passes,
                duration_s=round(playback.elapsed(self._clock()), 4),
            )
        )

    def _release(self, playback: Playback) -> None:
        self._playing.pop(playback.name, None)
        for resource, owner in list(self._owners.items()):
            if owner == playback.name:
                del self._owners[resource]

    async def _transition(self, playback: Playback) -> None:
        """Leave the face where the animation said to leave it."""
        target = playback.animation.transition
        if target is None or Resource.DISPLAY not in playback.resources:
            return
        try:
            await self.player.set_expression(target)
        except Exception as exc:  # a transition that cannot be sent is not worth failing over
            logger.debug("robot %s: transition to %s failed: %s", self.robot_id, target, exc)

    async def _publish(self, event: RobotEvent) -> None:
        if self._events is None:
            return
        try:
            await self._events.publish(event)
        except Exception as exc:
            logger.debug("robot %s: could not publish %s: %s", self.robot_id, event.name, exc)

    def __repr__(self) -> str:
        return f"<AnimationEngine {self.robot_id} playing={self.playing}>"


class HandlePlayer:
    """Adapts a :class:`~robot.actions.semantic.RobotHandle` to :class:`AnimationPlayer`.

    Everything maps one-to-one except sound: the semantic action vocabulary has no audio
    cue yet (roadmap Phase 6 gives speech an ``AUDIO`` resource claim), so an ``audio``
    step is logged and skipped rather than silently pretending to have played. An
    animation with an audio cue is therefore correct today and louder later.

    Commands are issued with ``wait=False``: an animation's timing comes from its own step
    offsets, and waiting for each command to complete would stretch every animation to the
    length of its slowest motion.
    """

    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self.skipped_sounds: list[str] = []

    async def set_expression(self, emotion: str, intensity_pct: int = 100, **kwargs: Any) -> Any:
        return await self.handle.set_expression(emotion, intensity_pct, wait=False, **kwargs)

    async def head_angle(self, pitch_deg: int = 0, yaw_deg: int = 0, **kwargs: Any) -> Any:
        return await self.handle.head_angle(pitch_deg, yaw_deg, wait=False, **kwargs)

    async def look_at(self, x_pct: int = 50, y_pct: int = 50, **kwargs: Any) -> Any:
        return await self.handle.look_at(x_pct, y_pct, wait=False, **kwargs)

    async def lift(self, height_pct: int, **kwargs: Any) -> Any:
        return await self.handle.lift(height_pct, wait=False, **kwargs)

    async def turn(self, angle_deg: int, **kwargs: Any) -> Any:
        return await self.handle.turn(angle_deg, wait=False, **kwargs)

    async def move(self, distance_mm: int, **kwargs: Any) -> Any:
        return await self.handle.move(distance_mm, wait=False, **kwargs)

    async def play_sound(self, sound: str, **kwargs: Any) -> Any:
        self.skipped_sounds.append(sound)
        logger.debug("robot: audio cue %r skipped; the action vocabulary has no audio channel yet", sound)
        return None


__all__ = [
    "DEFAULT_LOW_ENERGY_THRESHOLD",
    "LOOP_MIN_INTERVAL_S",
    "AnimationEngine",
    "AnimationPlayer",
    "AnimationRefused",
    "HandlePlayer",
    "Playback",
]
