"""What the robot is doing with its ears and its mouth, as five states and four edges.

    machine = VoiceStateMachine("nilo-sim-01", events=bus)
    await machine.transition_to(AudioState.LISTENING)

``IDLE``         nothing is happening
``LISTENING``    a microphone is open and somebody may be talking
``THINKING``     the utterance is being answered — the model, and any tools it calls
``SPEAKING``     audio is being streamed to the robot's speaker
``INTERRUPTED``  speech stopped early because somebody talked over it

The state machine exists so there is **one** answer to "is this robot talking?". The
inherited session has several: ``client_is_speaking``, ``client_abort``, the TTS queue's
own ``tts_sentence_type``, and whatever the device believes. None of them is wrong; none of
them is the whole picture either, and a barge-in that reads a different one from the one
the speaker wrote is a robot that talks over the person who interrupted it.

Transitions are checked, not assumed. An illegal transition is refused and logged rather
than applied, because the interesting bugs in an audio pipeline are the ones where two
paths both think they own the mouth.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

from robot.events.types import AudioStateChanged

logger = logging.getLogger(__name__)

#: How many transitions to keep for the management API and for a failing test to read.
HISTORY = 32


class AudioState(str, Enum):
    """The five states of the voice loop."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"

    @property
    def is_busy(self) -> bool:
        """Whether the robot is in the middle of a turn."""
        return self in (AudioState.LISTENING, AudioState.THINKING, AudioState.SPEAKING)


#: Where each state may go. Deliberately small: every edge here is one somebody can name.
#:
#: ``SPEAKING -> LISTENING`` is missing on purpose. Speech that ends because somebody
#: talked over it goes through ``INTERRUPTED`` first, so "was that reply cut off?" is a
#: question the history can answer.
ALLOWED_TRANSITIONS: dict[AudioState, frozenset[AudioState]] = {
    AudioState.IDLE: frozenset({AudioState.LISTENING, AudioState.THINKING, AudioState.SPEAKING}),
    AudioState.LISTENING: frozenset({AudioState.IDLE, AudioState.THINKING, AudioState.SPEAKING}),
    AudioState.THINKING: frozenset({AudioState.IDLE, AudioState.SPEAKING, AudioState.INTERRUPTED}),
    AudioState.SPEAKING: frozenset({AudioState.IDLE, AudioState.INTERRUPTED}),
    AudioState.INTERRUPTED: frozenset({AudioState.IDLE, AudioState.LISTENING}),
}


def can_transition(source: AudioState, target: AudioState) -> bool:
    """Whether ``source -> target`` is legal. A self-transition always is (it is a no-op)."""
    return source is target or target in ALLOWED_TRANSITIONS[source]


class VoiceStateMachine:
    """One robot's audio state, with a checked transition table and an event per change."""

    def __init__(
        self,
        robot_id: str,
        *,
        events: Any = None,
        on_change: Callable[[AudioState, AudioState], Awaitable[None] | None] | None = None,
    ) -> None:
        self.robot_id = robot_id
        self._events = events
        self._on_change = on_change
        self._state = AudioState.IDLE
        self._lock = asyncio.Lock()
        #: Every transition, newest last, bounded. ``(from, to, detail)``.
        self.history: list[tuple[AudioState, AudioState, str]] = []

    @property
    def state(self) -> AudioState:
        return self._state

    @property
    def speaking(self) -> bool:
        return self._state is AudioState.SPEAKING

    @property
    def path(self) -> tuple[AudioState, ...]:
        """The states passed through, oldest first. What a test asserts on."""
        return tuple(target for _, target, _ in self.history)

    async def transition_to(self, target: AudioState, *, detail: str = "") -> bool:
        """Move to ``target``. Returns whether the move happened.

        A refused transition is a logged ``False``, not an exception: the caller is an
        audio path, and an exception there loses a turn that could have continued.
        """
        async with self._lock:
            source = self._state
            if source is target:
                return False
            if not can_transition(source, target):
                logger.warning(
                    "robot %s: refusing the audio transition %s -> %s (%s)",
                    self.robot_id,
                    source.value,
                    target.value,
                    detail or "no detail",
                )
                return False
            self._state = target
            self.history.append((source, target, detail))
            del self.history[:-HISTORY]
        await self._publish(source, target, detail)
        if self._on_change is not None:
            result = self._on_change(source, target)
            if asyncio.iscoroutine(result):
                await result
        return True

    async def reset(self, *, detail: str = "reset") -> None:
        """Force the state back to ``IDLE``, whatever it was. For teardown only."""
        async with self._lock:
            source, self._state = self._state, AudioState.IDLE
            if source is AudioState.IDLE:
                return
            self.history.append((source, AudioState.IDLE, detail))
            del self.history[:-HISTORY]
        await self._publish(source, AudioState.IDLE, detail)

    async def _publish(self, source: AudioState, target: AudioState, detail: str) -> None:
        if self._events is None:
            return
        try:
            await self._events.publish(
                AudioStateChanged(
                    robot_id=self.robot_id,
                    previous=source.value,
                    state=target.value,
                    detail=detail,
                )
            )
        except Exception as exc:  # an unpublishable event must not stall the audio path
            logger.warning("robot %s: publishing an audio state change failed: %s", self.robot_id, exc)

    def __repr__(self) -> str:
        return f"<VoiceStateMachine {self.robot_id} {self._state.value}>"


__all__ = ["ALLOWED_TRANSITIONS", "HISTORY", "AudioState", "VoiceStateMachine", "can_transition"]
