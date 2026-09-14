"""The face, following the conversation — and knowing when to leave it alone.

    coordinator = ExpressionCoordinator("nilo-sim-01", handle, animations=engine)
    await coordinator.on_state(AudioState.IDLE, AudioState.LISTENING)

What the robot shows while it is talking to somebody:

``LISTENING``     attentive — the robot is waiting for the rest of the sentence
``THINKING``      curious — the model is working, and a still face reads as a hang
``SPEAKING``      a subtle speaking animation, *if the library has one*
``INTERRUPTED``   back to attentive: the person is talking again
``IDLE``          neutral
a failed turn     confused

**Avoid constant mechanical animation.** This is the whole design problem: a face driven
directly from an audio state machine changes on every transition, and a robot whose eyes
twitch four times per sentence is worse company than one that does nothing. Three rules
follow:

* **Nothing is sent when nothing changed.** The coordinator remembers what the face is
  showing and skips a repeat.
* **A minimum interval between changes.** Two transitions inside
  :data:`MIN_INTERVAL_S` produce one expression, not two — and the *last* one wins, so a
  fast LISTENING → THINKING → SPEAKING run settles on speaking rather than flickering
  through all three.
* **A missing animation is silence, not a fallback.** If the library has no ``speaking``
  animation the face simply keeps whatever it had. Substituting something else is how a
  robot ends up looking surprised every time it answers a question.

The coordinator commands through the same semantic handle as everything else, so every
expression it sets is still filtered by the safety policy. It cannot reach a device.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from robot.voice.state import AudioState

logger = logging.getLogger(__name__)

#: The least time between two face changes, in seconds.
MIN_INTERVAL_S = 0.6

#: What each audio state puts on the face. Every value is in
#: :data:`robot.animation.library.EXPRESSIONS`; a value that is not would be refused by
#: the device and show nothing.
STATE_EXPRESSIONS: dict[AudioState, str] = {
    AudioState.IDLE: "neutral",
    AudioState.LISTENING: "focused",
    AudioState.THINKING: "curious",
    AudioState.INTERRUPTED: "focused",
}

#: Animations played for a state, when the library has them. ``SPEAKING`` is the only one:
#: a talking face is worth a small loop, and everything else is a still expression.
STATE_ANIMATIONS: dict[AudioState, str] = {
    AudioState.SPEAKING: "speaking",
}

#: What the face shows when a turn fails — no model, a timeout, a refused tool.
FAILURE_EXPRESSION = "confused"


class ExpressionCoordinator:
    """Drives the face from the voice loop, sparingly.

    Holds no reference to a device: ``robot`` is a semantic handle and ``animations`` an
    animation engine, both of which go through the action executor.
    """

    def __init__(
        self,
        robot_id: str,
        robot: Any,
        *,
        animations: Any = None,
        clock: Callable[[], float] = time.monotonic,
        min_interval_s: float = MIN_INTERVAL_S,
        enabled: bool = True,
    ) -> None:
        self.robot_id = robot_id
        self.robot = robot
        self.animations = animations
        self.enabled = enabled
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._showing = ""
        self._last_change = float("-inf")
        self._pending: str | None = None
        #: Every expression actually commanded, in order. For tests and the dashboard.
        self.shown: list[str] = []

    @property
    def showing(self) -> str:
        return self._showing

    async def on_state(self, previous: AudioState, current: AudioState) -> None:
        """React to one audio-state transition. Never raises."""
        animation = STATE_ANIMATIONS.get(current)
        if animation is not None and await self._play(animation):
            return
        wanted = STATE_EXPRESSIONS.get(current)
        if wanted is None:
            return
        await self.show(wanted)

    async def on_failure(self, detail: str = "") -> None:
        """The turn went wrong. Say so with the face, once."""
        logger.debug("robot %s: showing %s (%s)", self.robot_id, FAILURE_EXPRESSION, detail or "failure")
        await self.show(FAILURE_EXPRESSION)

    async def on_tool(self, tool_name: str) -> None:
        """A tool is running. Play an animation for it if the library has one, else nothing.

        Deliberately no expression fallback: "the robot is fetching your battery level" is
        not worth a face change, and a face change per tool call is the mechanical
        twitching this module exists to avoid.
        """
        await self._play(f"tool_{tool_name}")

    async def show(self, emotion: str, *, force: bool = False) -> bool:
        """Set the face. Returns whether anything was sent.

        ``force`` skips the rate limit but not the "already showing it" check: a safety
        expression should not wait half a second, and it also should not be re-sent.
        """
        if not self.enabled or not emotion:
            return False
        if emotion == self._showing:
            return False
        now = self._clock()
        if not force and now - self._last_change < self.min_interval_s:
            # Remember it rather than dropping it: the last state in a fast run is the one
            # that should end up on the face, and :meth:`settle` puts it there.
            self._pending = emotion
            return False
        self._pending = None
        self._last_change = now
        self._showing = emotion
        self.shown.append(emotion)
        try:
            await self.robot.set_expression(emotion, wait=False)
        except Exception as exc:  # the face is not worth failing a conversation over
            logger.warning("robot %s: setting the expression failed: %s", self.robot_id, exc)
            return False
        return True

    async def settle(self) -> bool:
        """Apply the expression the rate limit held back, if there is one.

        Called when the turn ends: a fast LISTENING → THINKING → SPEAKING run inside one
        interval should leave the robot showing the last of them, not the first.
        """
        pending, self._pending = self._pending, None
        if pending is None:
            return False
        self._last_change = float("-inf")
        return await self.show(pending)

    async def _play(self, name: str) -> bool:
        """Play an animation if the library has one by that name. Never substitutes."""
        engine = self.animations
        if not self.enabled or engine is None:
            return False
        library = getattr(engine, "library", None)
        if library is None or library.get(name) is None:
            return False
        try:
            return await engine.play(name) is not None
        except Exception as exc:
            logger.warning("robot %s: playing %s failed: %s", self.robot_id, name, exc)
            return False


__all__ = [
    "FAILURE_EXPRESSION",
    "MIN_INTERVAL_S",
    "STATE_ANIMATIONS",
    "STATE_EXPRESSIONS",
    "ExpressionCoordinator",
]
