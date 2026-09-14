"""One path from "something wants the robot to talk" to the robot talking.

    intent = SpeakIntent(reason="greeting", target_person_id="ahmad", style="excited")
    decision = await arbiter.request(intent)

Every source of speech goes through :class:`SpeechArbiter`: the behaviour engine, a safety
announcement, an ambient remark, and the reply to something a person said. Nothing else
calls a model, and nothing else starts a text-to-speech stream.

That single path exists because the alternative is a robot with several mouths. A
behaviour that calls the model directly races the reply to the person who is mid-sentence;
two of them race each other; and neither knows that the third one is a low-battery warning
that should have won. Arbitration needs one place that can see all of them, so there is
one place.

Four priorities, and the ordering is the whole design:

``SAFETY``         "I am about to fall off the table". Interrupts anything.
``USER_RESPONSE``  the answer to something a person just said. Interrupts ambient chatter.
``BEHAVIOR``       the behaviour engine decided this was worth saying.
``AMBIENT``        idle noise. Dropped the moment anything else wants to speak.

A rejected intent is a returned :class:`SpeechDecision`, never an exception: a behaviour
that asked to say hello and was told the robot is already answering a question has not
malfunctioned.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Protocol
from uuid import uuid4

from robot.agent.permissions import TurnOrigin
from robot.events.types import RobotEvent, SpeechInterrupted, SpeechRequested

logger = logging.getLogger(__name__)

#: How long the same reason must wait before it may speak again, in seconds. A behaviour
#: that re-proposes "greeting" every tick is a behaviour that would otherwise greet you
#: forty times a minute.
DEFAULT_REASON_COOLDOWN_S = 20.0

#: How long a speaker gets to stop on its own before it is cancelled, in seconds.
INTERRUPT_GRACE_S = 1.0


class SpeechPriority(IntEnum):
    """Who wins when two things want to speak at once. Higher interrupts lower."""

    AMBIENT = 0
    BEHAVIOR = 10
    USER_RESPONSE = 50
    SAFETY = 100

    @property
    def origin(self) -> TurnOrigin:
        """The turn origin an intent at this priority implies, absent anything better."""
        return TurnOrigin.USER if self is SpeechPriority.USER_RESPONSE else TurnOrigin.BEHAVIOR


@dataclass(frozen=True)
class SpeakIntent:
    """A structured request for the robot to say something.

    ``reason`` is a short stable key ("greeting", "low_battery", "answer"), not a
    sentence: it is what the cooldown is keyed on, what shows up in events, and what the
    model is told it is being asked to do.

    ``text`` is the escape hatch for speech that must not be paraphrased — a safety line
    is said exactly as written, with no model in the path.
    """

    reason: str
    priority: SpeechPriority = SpeechPriority.BEHAVIOR
    target_person_id: str | None = None
    style: str = "neutral"
    #: Say exactly this, with no model call. Empty means "ask the agent to compose it".
    text: str = ""
    #: What the model should be asked, when ``text`` is empty. Empty means the reason is
    #: descriptive enough on its own.
    prompt: str = ""
    intent_id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def origin(self) -> TurnOrigin:
        return self.priority.origin

    @property
    def verbatim(self) -> bool:
        return bool(self.text)

    def describe(self) -> str:
        who = f" to {self.target_person_id}" if self.target_person_id else ""
        return f"{self.reason}{who} ({self.priority.name.lower()}, {self.style})"


@dataclass(frozen=True, slots=True)
class SpeechDecision:
    """What the arbiter did with an intent."""

    accepted: bool
    intent_id: str
    reason: str = ""
    interrupted: str | None = None

    @classmethod
    def accept(cls, intent: SpeakIntent, interrupted: str | None = None) -> SpeechDecision:
        return cls(True, intent.intent_id, interrupted=interrupted)

    @classmethod
    def reject(cls, intent: SpeakIntent, reason: str) -> SpeechDecision:
        return cls(False, intent.intent_id, reason)


class Speaker(Protocol):
    """What the arbiter needs from whatever actually produces speech.

    A structural type so the arbiter can be tested with a recorder, and so it has no
    opinion about whether speech is a text-to-speech stream, a log line, or a model call.
    """

    async def speak(self, intent: SpeakIntent) -> None: ...

    async def interrupt(self, reason: str) -> None: ...


class SpeechArbiter:
    """Serializes speech for one robot, with priority preemption and a per-reason cooldown.

    Not a queue: a robot that says the four things it wanted to say twenty seconds late is
    worse than a robot that says the important one now. A lower-priority intent that
    arrives while something is speaking is dropped, and told why.
    """

    def __init__(
        self,
        robot_id: str,
        speaker: Speaker,
        *,
        events: Any = None,
        clock: Callable[[], float] = time.monotonic,
        reason_cooldown_s: float = DEFAULT_REASON_COOLDOWN_S,
    ) -> None:
        self.robot_id = robot_id
        self.speaker = speaker
        self.reason_cooldown_s = reason_cooldown_s
        self._events = events
        self._clock = clock
        self._current: SpeakIntent | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_spoken: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def speaking(self) -> bool:
        return self._current is not None

    @property
    def current(self) -> SpeakIntent | None:
        return self._current

    async def request(self, intent: SpeakIntent, *, wait: bool = False) -> SpeechDecision:
        """Ask the robot to say something. Returns without waiting unless told to.

        ``wait=True`` is for a caller that needs the speech to have finished — a test, or a
        shutdown announcement. A behaviour must not use it: it would hold the behaviour
        engine's tick for the length of a sentence.
        """
        async with self._lock:
            if self._closed:
                return await self._rejected(intent, "the speech arbiter is closed")
            cooling = self._cooling_down(intent)
            if cooling is not None:
                return await self._rejected(intent, cooling)
            interrupted: str | None = None
            current = self._current
            if current is not None:
                if intent.priority <= current.priority:
                    return await self._rejected(
                        intent, f"the robot is already saying something ({current.reason})"
                    )
                interrupted = current.reason
                await self._cancel(f"preempted by {intent.reason}")
            self._last_spoken[intent.reason] = self._clock()
            self._current = intent
            self._task = asyncio.create_task(
                self._run(intent), name=f"robot-speech-{self.robot_id}-{intent.reason}"
            )
            task = self._task
            decision = SpeechDecision.accept(intent, interrupted)
            await self._publish(
                SpeechRequested(
                    robot_id=self.robot_id,
                    intent_id=intent.intent_id,
                    reason=intent.reason,
                    priority=int(intent.priority),
                    accepted=True,
                    interrupted=interrupted,
                )
            )
        if wait:
            await asyncio.gather(task, return_exceptions=True)
        return decision

    async def interrupt(self, reason: str = "interrupted") -> SpeakIntent | None:
        """Stop whatever the robot is saying. Returns what was interrupted, if anything.

        This is the barge-in entry point: a person started talking, so the robot stops.
        """
        async with self._lock:
            current = self._current
            if current is None:
                return None
            await self._cancel(reason)
            return current

    async def wait_idle(self) -> None:
        """Wait until nothing is being said. For tests and for shutdown."""
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._cancel("the speech arbiter is closing")

    # -- internals ---------------------------------------------------------------------------

    async def _rejected(self, intent: SpeakIntent, why: str) -> SpeechDecision:
        logger.debug("robot %s: not saying %s — %s", self.robot_id, intent.describe(), why)
        await self._publish(
            SpeechRequested(
                robot_id=self.robot_id,
                intent_id=intent.intent_id,
                reason=intent.reason,
                priority=int(intent.priority),
                accepted=False,
                rejected_because=why,
            )
        )
        return SpeechDecision.reject(intent, why)

    async def _publish(self, event: RobotEvent) -> None:
        if self._events is None:
            return
        try:
            await self._events.publish(event)
        except Exception as exc:  # an unpublishable event must not stop the robot talking
            logger.warning("robot %s: publishing %s failed: %s", self.robot_id, type(event).__name__, exc)

    def _cooling_down(self, intent: SpeakIntent) -> str | None:
        """Whether this reason spoke too recently. Safety is never cooled down."""
        if intent.priority is SpeechPriority.SAFETY or self.reason_cooldown_s <= 0:
            return None
        last = self._last_spoken.get(intent.reason)
        if last is None:
            return None
        elapsed = self._clock() - last
        if elapsed >= self.reason_cooldown_s:
            return None
        return f"{intent.reason} was said {elapsed:.0f}s ago; the cooldown is {self.reason_cooldown_s:.0f}s"

    async def _cancel(self, reason: str) -> None:
        """Stop what is being said. The speaker is told *before* its task is cancelled.

        The order matters more than it looks. A hard cancel first would unwind the speaker
        out of the middle of a turn, and the conversational state that turn was building —
        what the robot actually said out loud before it was cut off — would be lost with
        it. So the speaker is asked to stop cooperatively, given
        :data:`INTERRUPT_GRACE_S` to settle, and only then cancelled.
        """
        task, self._task = self._task, None
        interrupted, self._current = self._current, None
        if interrupted is not None:
            await self._publish(
                SpeechInterrupted(robot_id=self.robot_id, intent_id=interrupted.intent_id, reason=reason)
            )
        try:
            await self.speaker.interrupt(reason)
        except Exception as exc:  # an uncooperative speaker must not wedge the arbiter
            logger.warning("robot %s: interrupting speech failed: %s", self.robot_id, exc)
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=INTERRUPT_GRACE_S)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "robot %s: speech did not stop within %.1fs; cancelling it",
                self.robot_id,
                INTERRUPT_GRACE_S,
            )
            task.cancel()
        except Exception:
            pass

    async def _run(self, intent: SpeakIntent) -> None:
        if self._current is not intent:
            # Preempted between ``create_task`` and the first line of this coroutine.
            # Starting the speaker now would say something the arbiter already decided
            # against, a beat after the thing that outranked it began.
            logger.debug("robot %s: %s was preempted before it started", self.robot_id, intent.reason)
            return
        try:
            await self.speaker.speak(intent)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("robot %s: speaking %s failed", self.robot_id, intent.reason)
        finally:
            if self._current is intent:
                self._current = None
                self._task = None


class RecordingSpeaker:
    """A :class:`Speaker` that records instead of speaking. For tests and dry runs.

    It honours an interrupt the way a real speaker must: an utterance that was stopped
    part-way through is not recorded as having been said. A speaker that ignored the
    interrupt and finished anyway would be a robot that talks over the person who
    interrupted it, which is the failure the arbiter exists to prevent.
    """

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.spoken: list[SpeakIntent] = []
        self.interruptions: list[str] = []
        self._speaking: str | None = None
        self._stopped: str | None = None

    async def speak(self, intent: SpeakIntent) -> None:
        self._speaking = intent.intent_id
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self._speaking = None
        if self._stopped == intent.intent_id:
            return
        self.spoken.append(intent)

    async def interrupt(self, reason: str) -> None:
        self.interruptions.append(reason)
        self._stopped = self._speaking


#: The signature a behaviour uses to ask for speech. Behaviours are handed one of these
#: rather than the arbiter itself, so a behaviour cannot interrupt or close it.
SpeechRequest = Callable[[SpeakIntent], Awaitable[SpeechDecision]]


__all__ = [
    "DEFAULT_REASON_COOLDOWN_S",
    "INTERRUPT_GRACE_S",
    "RecordingSpeaker",
    "SpeakIntent",
    "SpeechArbiter",
    "SpeechDecision",
    "SpeechPriority",
    "SpeechRequest",
    "Speaker",
]
