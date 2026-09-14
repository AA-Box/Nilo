"""The complete path: an utterance in, a robot that moves and answers, out.

    loop = VoiceLoop("nilo-sim-01", agent, sink, events=bus, animations=engine)
    turn = await loop.on_utterance("come closer", person_id="ahmad")

    microphone -> VAD -> ASR -> person -> agent -> tools -> model -> TTS -> speaker
                                  (inherited)      (robot/agent)   (inherited)

Everything upstream of ``on_utterance`` and everything downstream of the sink is the
inherited pipeline: Silero VAD, the streaming ASR providers, the sentence-chunked TTS
queue and the Opus encoder over the device WebSocket. None of it is reimplemented here.
What is here is the part that did not exist: the piece in the middle that decides who is
allowed to talk, what the face does while they do, and what happens when somebody talks
over the robot.

**One mouth.** Every sentence the robot says — an answer, a greeting a behaviour asked
for, a safety warning — goes through :class:`~robot.agent.speech.SpeechArbiter` and then
through one sink guarded by one lock. Two simultaneous streams for one robot is not a race
this code can lose, because the second one waits for the first to let go.

**Barge-in is a first-class transition, not a cancellation.** ``SPEAKING -> INTERRUPTED ->
LISTENING``: the text-to-speech stream is cancelled, the conversation is kept, the partial
reply is recorded as said, and the robot is listening again before the person has finished
their sentence.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from robot.agent.agent import AgentTurn, RobotAgent
from robot.agent.speech import (
    DEFAULT_REASON_COOLDOWN_S,
    SpeakIntent,
    SpeechArbiter,
    SpeechDecision,
    SpeechPriority,
)
from robot.events.types import SpeechFinished, SpeechStarted, UtteranceRecognized
from robot.voice.expression import ExpressionCoordinator
from robot.voice.state import AudioState, VoiceStateMachine

logger = logging.getLogger(__name__)

#: The reason a user answer is filed under. One key, so the per-reason cooldown never
#: applies to answering a person — a cooldown on that would be a robot that ignores you.
ANSWER_REASON = "answer"


class SpeechSink(Protocol):
    """Whatever turns text into sound. The inherited TTS pipeline, or a recorder.

    Structural, so the loop has no opinion about whether the other end is an Opus stream
    over a WebSocket or a list in a test.
    """

    async def start(self, intent: SpeakIntent) -> None: ...

    async def say(self, text: str) -> None: ...

    async def finish(self) -> None: ...

    async def cancel(self, reason: str) -> None: ...


class RecordingSink:
    """A sink that records instead of speaking, and counts overlapping streams.

    ``max_concurrent`` is the assertion: a robot has one speaker, so it must never rise
    above one.
    """

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.streams: list[list[str]] = []
        self.cancelled: list[str] = []
        self.finished = 0
        self.active = 0
        self.max_concurrent = 0

    @property
    def spoken(self) -> list[str]:
        """Every completed or partial utterance, joined."""
        return ["".join(stream) for stream in self.streams]

    async def start(self, intent: SpeakIntent) -> None:
        self.active += 1
        self.max_concurrent = max(self.max_concurrent, self.active)
        self.streams.append([])

    async def say(self, text: str) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.streams:
            self.streams[-1].append(text)

    async def finish(self) -> None:
        self.finished += 1
        self.active = max(0, self.active - 1)

    async def cancel(self, reason: str) -> None:
        self.cancelled.append(reason)
        self.active = max(0, self.active - 1)


class VoiceSpeaker:
    """The arbiter's :class:`~robot.agent.speech.Speaker`: agent in, audio out.

    It is what makes "answer the person" and "say hello because a behaviour asked" the
    same code path with a different priority, which is what makes them arbitrable at all.
    """

    def __init__(self, loop: VoiceLoop) -> None:
        self.loop = loop
        self._stream = asyncio.Lock()

    async def speak(self, intent: SpeakIntent) -> None:
        # The lock, not just the arbiter: an intent that preempted another one starts
        # while the cancelled task is still unwinding out of the sink.
        async with self._stream:
            await self.loop._speak(intent)

    async def interrupt(self, reason: str) -> None:
        await self.loop._interrupt(reason)


class VoiceLoop:
    """The voice and interaction loop for one robot."""

    def __init__(
        self,
        robot_id: str,
        agent: RobotAgent,
        sink: SpeechSink,
        *,
        events: Any = None,
        animations: Any = None,
        expressions: ExpressionCoordinator | None = None,
        arbiter: SpeechArbiter | None = None,
        reason_cooldown_s: float = DEFAULT_REASON_COOLDOWN_S,
    ) -> None:
        self.robot_id = robot_id
        self.agent = agent
        self.sink = sink
        self._events = events
        self.states = VoiceStateMachine(robot_id, events=events, on_change=self._on_state)
        self.expressions = expressions or ExpressionCoordinator(
            robot_id, agent.toolkit.handle(), animations=animations
        )
        self.speaker = VoiceSpeaker(self)
        self.arbiter = arbiter or SpeechArbiter(
            robot_id, self.speaker, events=events, reason_cooldown_s=reason_cooldown_s
        )
        self.arbiter.speaker = self.speaker
        #: The most recent completed turn. What ``on_utterance`` returns.
        self.last_turn: AgentTurn | None = None
        self._closed = False

    # -- state ---------------------------------------------------------------------------------

    @property
    def state(self) -> AudioState:
        return self.states.state

    @property
    def speaking(self) -> bool:
        return self.states.speaking

    async def listening(self, *, detail: str = "the microphone is open") -> bool:
        """The device opened its microphone. ``IDLE -> LISTENING``."""
        return await self.states.transition_to(AudioState.LISTENING, detail=detail)

    async def idle(self, *, detail: str = "") -> bool:
        return await self.states.transition_to(AudioState.IDLE, detail=detail)

    # -- the turn ---------------------------------------------------------------------------------

    async def on_utterance(
        self, text: str, *, person_id: str | None = None, speaker: str = ""
    ) -> AgentTurn | None:
        """Answer something a person said. The main entry point of the whole loop.

        Returns the turn, or ``None`` when the arbiter refused — which for a user answer
        only happens when something at :attr:`SpeechPriority.SAFETY` is talking.
        """
        if self._closed or not text.strip():
            return None
        await self._publish(
            UtteranceRecognized(
                robot_id=self.robot_id, text=text, person_id=person_id, speaker=speaker
            )
        )
        intent = SpeakIntent(
            reason=ANSWER_REASON,
            priority=SpeechPriority.USER_RESPONSE,
            target_person_id=person_id,
            prompt=text,
        )
        decision = await self.arbiter.request(intent, wait=True)
        if not decision.accepted:
            logger.info("robot %s: not answering — %s", self.robot_id, decision.reason)
            return None
        return self.last_turn

    async def speak(self, intent: SpeakIntent) -> SpeechDecision:
        """Say something nobody asked for: a behaviour's greeting, a safety warning."""
        return await self.arbiter.request(intent)

    async def barge_in(self, reason: str = "the person started speaking") -> bool:
        """Somebody talked over the robot. Stop, keep the conversation, listen.

        Returns whether there was anything to interrupt. Safe to call when the robot is
        silent, which is what makes it safe to wire straight into the inherited abort
        handler.
        """
        if self.arbiter.current is None and not self.state.is_busy:
            return False
        # Before the interrupt, not after: the speech task settles inside
        # ``arbiter.interrupt``, and a turn that has already reached IDLE cannot then move
        # to INTERRUPTED.
        await self.states.transition_to(AudioState.INTERRUPTED, detail=reason)
        interrupted = await self.arbiter.interrupt(reason)
        await self.states.transition_to(AudioState.LISTENING, detail="listening after a barge-in")
        return interrupted is not None

    async def aclose(self) -> None:
        self._closed = True
        await self.arbiter.aclose()
        await self.states.reset(detail="the voice loop is closing")

    # -- internals ------------------------------------------------------------------------------------

    async def _speak(self, intent: SpeakIntent) -> None:
        """One whole utterance: think, then stream it. Called by the arbiter's speaker."""
        await self.states.transition_to(AudioState.THINKING, detail=intent.reason)
        started = False

        async def emit(chunk: str) -> None:
            nonlocal started
            if not started:
                started = True
                await self.states.transition_to(AudioState.SPEAKING, detail=intent.reason)
                await self.sink.start(intent)
                await self._publish(
                    SpeechStarted(
                        robot_id=self.robot_id,
                        intent_id=intent.intent_id,
                        reason=intent.reason,
                        priority=int(intent.priority),
                    )
                )
            await self.sink.say(chunk)

        try:
            turn = await self._think(intent, emit)
        except asyncio.CancelledError:
            # Cancelled mid-sentence. `_interrupt` has already told the sink; nothing to
            # unwind here beyond letting the state machine catch up.
            raise
        self.last_turn = turn
        if turn.tool_calls:
            for tool in turn.tool_calls:
                await self.expressions.on_tool(tool)
        if not started and turn.text:
            await emit(turn.text)
        if started:
            if not turn.interrupted:
                await self.sink.finish()
            await self._publish(
                SpeechFinished(
                    robot_id=self.robot_id,
                    intent_id=intent.intent_id,
                    reason=intent.reason,
                    text=turn.text,
                    interrupted=turn.interrupted,
                )
            )
        if turn.used_fallback or turn.refusals:
            await self.expressions.on_failure(turn.fallback or "a tool was refused")
        if not turn.interrupted:
            # An interrupted turn is not over: :meth:`barge_in` owns the state from here,
            # and forcing IDLE would undo the transition to LISTENING it is about to make.
            await self.states.transition_to(AudioState.IDLE, detail="the turn is over")
        await self.expressions.settle()

    async def _think(self, intent: SpeakIntent, emit: Any) -> AgentTurn:
        """Produce the words. A verbatim intent skips the model entirely."""
        if intent.verbatim:
            await emit(intent.text)
            return AgentTurn(turn_id=intent.intent_id, text=intent.text)
        if intent.reason == ANSWER_REASON and intent.prompt:
            return await self.agent.respond(
                intent.prompt, person_id=intent.target_person_id, on_text=emit
            )
        turn = await self.agent.speak_intent(intent)
        if turn.text:
            await emit(turn.text)
        return turn

    async def _interrupt(self, reason: str) -> None:
        await self.sink.cancel(reason)
        await self.agent.interrupt(reason)

    async def _on_state(self, previous: AudioState, current: AudioState) -> None:
        await self.expressions.on_state(previous, current)

    async def _publish(self, event: Any) -> None:
        if self._events is None:
            return
        try:
            await self._events.publish(event)
        except Exception as exc:  # an unpublishable event must not stall the audio path
            logger.warning("robot %s: publishing %s failed: %s", self.robot_id, type(event).__name__, exc)

    def __repr__(self) -> str:
        return f"<VoiceLoop {self.robot_id} {self.state.value}>"


__all__ = ["ANSWER_REASON", "RecordingSink", "SpeechSink", "VoiceLoop", "VoiceSpeaker"]
