"""The complete voice loop, end to end, with nothing real but the robot.

The headline test is the one the design note asks for:

    a person says "come closer"
      -> fake ASR produces the text
      -> the agent calls robot.move(distance_mm=250)
      -> the *simulated robot* executes it: physics, pose, completion notification
      -> the robot answers "Okay"

and every transition and event along the way is asserted, not assumed.

Four fakes and one real thing. Fake: the microphone (a list of strings), the ASR (it
hands those strings to the loop), the model (a script), and the speaker (a recorder that
counts overlapping streams). Real: the robot — ``tests/robot/simulated.py`` is the
simulator's own physics, sensor model and tool table, with the WebSocket taken out — plus
the executor, the safety policy, the world model and the event bus.
"""
from __future__ import annotations

import asyncio

import pytest

from robot.agent.agent import RobotAgent
from robot.agent.speech import SpeakIntent, SpeechPriority
from robot.animation.engine import AnimationEngine, HandlePlayer
from robot.animation.library import AnimationLibrary
from robot.animation.model import Animation, AnimationStep, Channel
from robot.events.types import (
    ActionFinished,
    AgentTurnCompleted,
    AudioStateChanged,
    SpeechFinished,
    SpeechStarted,
    UtteranceRecognized,
)
from robot.runtime import RobotRuntime
from robot.simulator.world import Cliff
from robot.voice.expression import FAILURE_EXPRESSION, ExpressionCoordinator
from robot.voice.loop import RecordingSink, VoiceLoop
from robot.voice.state import ALLOWED_TRANSITIONS, AudioState, VoiceStateMachine, can_transition
from tests.robot.conftest import ROBOT_ID, ScriptedLLM, tool_call, until
from tests.robot.simulated import SimulatedDevice


class FakeASR:
    """Microphone and speech recognition, as a list of things somebody said.

    The real path is Silero VAD into a streaming ASR provider; both are inherited and
    unchanged. What this stands in for is their *output*: a final transcript, and the
    person the voiceprint layer attributed it to.
    """

    def __init__(self, loop: VoiceLoop) -> None:
        self.loop = loop
        self.heard: list[tuple[str, str | None]] = []

    async def hears(self, text: str, *, person_id: str | None = None):
        await self.loop.listening()
        self.heard.append((text, person_id))
        return await self.loop.on_utterance(text, person_id=person_id, speaker=person_id or "")


@pytest.fixture
async def simulated():
    """A runtime with a real simulated robot on it: physics, sensors, motion timing."""
    runtime = RobotRuntime(discovery_timeout=1.0)
    device = await SimulatedDevice(ROBOT_ID).attach(runtime)
    try:
        yield runtime, device
    finally:
        await device.aclose()
        await runtime.aclose()


def build_loop(runtime, *turns, sink=None, cooldown=0.0, **agent_kwargs) -> tuple[VoiceLoop, RecordingSink]:
    agent = RobotAgent(ROBOT_ID, runtime, llm=ScriptedLLM(*turns), **agent_kwargs)
    recorder = sink or RecordingSink()
    loop = VoiceLoop(
        ROBOT_ID, agent, recorder, events=runtime.events, reason_cooldown_s=cooldown
    )
    return loop, recorder


def collect(runtime, event_type):
    seen: list = []
    runtime.events.subscribe(event_type, seen.append)
    return seen


# -- the whole path ---------------------------------------------------------------------------------


async def test_come_closer(simulated):
    """The end-to-end flow from the design note, with every transition verified."""
    runtime, device = simulated
    states = collect(runtime, AudioStateChanged)
    utterances = collect(runtime, UtteranceRecognized)
    starts = collect(runtime, SpeechStarted)
    finishes = collect(runtime, SpeechFinished)
    turns = collect(runtime, AgentTurnCompleted)
    actions = collect(runtime, ActionFinished)

    loop, sink = build_loop(
        runtime, [tool_call("robot_move", distance_mm=250)], "Okay."
    )
    asr = FakeASR(loop)
    start_x = device.state.x_m

    turn = await asr.hears("come closer", person_id="ahmad")

    # -- the model asked for a semantic action, and the robot ran it -------------------------
    assert turn is not None
    assert turn.tool_calls == ("robot_move",)
    assert device.called("robot.motion.move")[0]["distance_mm"] == 250
    moved = lambda: any(
        e.action.action_type.value == "move" and e.action.status.value == "succeeded" for e in actions
    )
    assert await until(moved, timeout=5.0), "the move action never completed"
    assert device.state.x_m == pytest.approx(start_x + 0.25, abs=0.02), "the robot did not actually move"

    # -- and then it answered ----------------------------------------------------------------
    assert turn.text == "Okay."
    assert sink.spoken == ["Okay."]
    assert sink.finished == 1
    assert sink.cancelled == []

    # -- every transition, in order ----------------------------------------------------------
    assert loop.states.path == (
        AudioState.LISTENING,
        AudioState.THINKING,
        AudioState.SPEAKING,
        AudioState.IDLE,
    )
    assert loop.state is AudioState.IDLE

    # -- and every event ---------------------------------------------------------------------
    await runtime.events.drain()
    assert [(e.previous, e.state) for e in states] == [
        ("idle", "listening"),
        ("listening", "thinking"),
        ("thinking", "speaking"),
        ("speaking", "idle"),
    ]
    assert [(e.text, e.person_id) for e in utterances] == [("come closer", "ahmad")]
    assert [e.reason for e in starts] == ["answer"]
    assert [(e.text, e.interrupted) for e in finishes] == [("Okay.", False)]
    assert [e.tool_calls for e in turns] == [("robot_move",)]


async def test_an_unsafe_request_is_refused_and_the_robot_says_so(simulated):
    """The same path, with the robot at the edge of a drop: it answers, it does not move."""
    runtime, device = simulated
    device.world.add_cliff(Cliff(id="edge", x0_m=0.62, y0_m=-0.4, x1_m=1.4, y1_m=0.8))
    device.state.refresh_sensors(device.world)
    await device.step(0.0)
    assert await until(lambda: device.state.cliff_detected, timeout=2.0)
    start_x = device.state.x_m

    loop, sink = build_loop(
        runtime,
        [tool_call("robot_move", distance_mm=250)],
        "I cannot, there is a drop in front of me.",
    )

    turn = await FakeASR(loop).hears("come closer", person_id="ahmad")

    assert turn is not None
    assert turn.refusals[0].refused_by == "safety:cliff_hazard"
    assert device.called("robot.motion.move") == ()
    assert device.state.x_m == pytest.approx(start_x, abs=1e-6)
    assert sink.spoken == ["I cannot, there is a drop in front of me."]


# -- barge-in -----------------------------------------------------------------------------------------


async def test_a_person_talking_over_the_robot_stops_it_and_it_listens(simulated):
    runtime, _ = simulated
    loop, sink = build_loop(runtime, "This is a long answer that will be cut off partway.")
    loop.agent.llm.delay_s = 0.01

    task = asyncio.create_task(loop.on_utterance("tell me a story"))
    assert await until(lambda: loop.state is AudioState.SPEAKING, timeout=3.0)

    interrupted = await loop.barge_in("the person started speaking")
    await task

    assert interrupted
    assert sink.cancelled == ["the person started speaking"]
    assert loop.state is AudioState.LISTENING
    assert AudioState.INTERRUPTED in loop.states.path
    # The conversation survived: what was said is in the transcript, marked.
    assert any("[interrupted]" in m["content"] for m in loop.agent.conversation.messages)


async def test_the_next_utterance_is_answered_after_a_barge_in(simulated):
    runtime, _ = simulated
    loop, sink = build_loop(runtime, "A long first answer that gets cut off.", "Second.")
    loop.agent.llm.delay_s = 0.01

    task = asyncio.create_task(loop.on_utterance("first"))
    assert await until(lambda: loop.state is AudioState.SPEAKING, timeout=3.0)
    await loop.barge_in()
    await task

    loop.agent.llm.delay_s = 0.0
    turn = await loop.on_utterance("second")

    assert turn is not None and turn.text == "Second."
    assert sink.spoken[-1] == "Second."


async def test_barging_in_on_a_silent_robot_is_a_no_op(simulated):
    runtime, _ = simulated
    loop, sink = build_loop(runtime, "Fine.")
    assert await loop.barge_in() is False
    assert sink.cancelled == []
    assert loop.state is AudioState.IDLE


async def test_a_partial_reply_is_reported_as_interrupted(simulated):
    runtime, _ = simulated
    finishes = collect(runtime, SpeechFinished)
    loop, _ = build_loop(runtime, "A sentence long enough to be cut in the middle of it.")
    loop.agent.llm.delay_s = 0.01

    task = asyncio.create_task(loop.on_utterance("talk"))
    assert await until(lambda: loop.state is AudioState.SPEAKING, timeout=3.0)
    await loop.barge_in()
    await task
    await runtime.events.drain()

    assert finishes and finishes[-1].interrupted is True


# -- one mouth ----------------------------------------------------------------------------------------


async def test_two_things_never_stream_to_the_speaker_at_once(simulated):
    runtime, _ = simulated
    sink = RecordingSink(delay_s=0.02)
    loop, _ = build_loop(runtime, "A reasonably long answer.", sink=sink)

    await asyncio.gather(
        loop.on_utterance("one"),
        loop.on_utterance("two"),
        loop.speak(SpeakIntent(reason="idle_remark", priority=SpeechPriority.AMBIENT)),
    )
    await loop.arbiter.wait_idle()

    assert sink.max_concurrent == 1, "two text-to-speech streams overlapped"


async def test_a_behaviour_cannot_talk_over_an_answer(simulated):
    runtime, _ = simulated
    sink = RecordingSink(delay_s=0.02)
    loop, _ = build_loop(runtime, "The answer.", sink=sink)

    task = asyncio.create_task(loop.on_utterance("a question"))
    await until(lambda: loop.state in (AudioState.THINKING, AudioState.SPEAKING), timeout=3.0)
    decision = await loop.speak(SpeakIntent(reason="greeting", priority=SpeechPriority.BEHAVIOR))
    await task

    assert not decision.accepted
    assert "already saying something" in decision.reason


async def test_a_safety_line_interrupts_an_answer_and_is_said_verbatim(simulated):
    runtime, _ = simulated
    sink = RecordingSink(delay_s=0.05)
    loop, _ = build_loop(runtime, "A long answer that safety will cut off.", sink=sink)
    loop.agent.llm.delay_s = 0.01

    task = asyncio.create_task(loop.on_utterance("a question"))
    assert await until(lambda: loop.state is AudioState.SPEAKING, timeout=3.0)
    decision = await loop.speak(
        SpeakIntent(reason="cliff", priority=SpeechPriority.SAFETY, text="Careful, there is a drop!")
    )
    await task
    await loop.arbiter.wait_idle()

    assert decision.accepted
    assert decision.interrupted == "answer"
    assert "Careful, there is a drop!" in sink.spoken
    assert sink.max_concurrent == 1


# -- the state machine --------------------------------------------------------------------------------


async def test_every_state_the_design_note_asks_for_exists():
    assert {state.value for state in AudioState} == {
        "idle",
        "listening",
        "thinking",
        "speaking",
        "interrupted",
    }


async def test_an_illegal_transition_is_refused_rather_than_applied():
    machine = VoiceStateMachine(ROBOT_ID)
    await machine.transition_to(AudioState.SPEAKING)

    assert not can_transition(AudioState.SPEAKING, AudioState.THINKING)
    assert await machine.transition_to(AudioState.THINKING) is False
    assert machine.state is AudioState.SPEAKING


async def test_speech_that_is_cut_off_goes_through_interrupted():
    """So that "was that reply cut off?" is a question the history can answer."""
    from_speaking = ALLOWED_TRANSITIONS[AudioState.SPEAKING]
    assert AudioState.LISTENING not in from_speaking
    assert AudioState.INTERRUPTED in from_speaking


# -- expression ------------------------------------------------------------------------------------------


async def test_the_face_follows_the_conversation(simulated):
    """Attentive while listening, curious while thinking, neutral when the turn is over."""
    runtime, _ = simulated
    loop, _ = build_loop(runtime, "Fine.")
    loop.expressions.min_interval_s = 0.0

    await FakeASR(loop).hears("hello")

    assert loop.expressions.shown == ["focused", "curious", "neutral"]


async def test_the_face_does_not_twitch_on_every_transition(simulated):
    """Four transitions inside one interval produce two face changes, not four.

    One when the turn starts, and one when it settles — and the settled one is the state
    the robot actually ended in, not the one it passed through first.
    """
    runtime, _ = simulated
    loop, _ = build_loop(runtime, "Fine.")
    loop.expressions.min_interval_s = 60.0

    await FakeASR(loop).hears("hello")

    assert len(loop.states.path) == 4
    assert loop.expressions.shown == ["focused", "neutral"]


async def test_the_held_back_expression_is_applied_when_the_turn_settles(simulated):
    runtime, _ = simulated
    coordinator = ExpressionCoordinator(ROBOT_ID, _Recorder(), min_interval_s=60.0)

    assert await coordinator.show("focused")
    assert not await coordinator.show("curious")
    assert await coordinator.settle()
    assert coordinator.shown == ["focused", "curious"]


async def test_the_same_expression_is_never_sent_twice():
    coordinator = ExpressionCoordinator(ROBOT_ID, _Recorder(), min_interval_s=0.0)
    assert await coordinator.show("focused")
    assert not await coordinator.show("focused")
    assert coordinator.shown == ["focused"]


async def test_a_failed_turn_shows_a_confused_face(simulated):
    runtime, _ = simulated
    agent = RobotAgent(ROBOT_ID, runtime, llm=None)
    loop = VoiceLoop(ROBOT_ID, agent, RecordingSink(), events=runtime.events)
    loop.expressions.min_interval_s = 0.0

    await loop.on_utterance("hello")

    assert FAILURE_EXPRESSION in loop.expressions.shown


async def test_a_speaking_animation_is_played_when_the_library_has_one(simulated):
    runtime, _ = simulated
    recorder = _Recorder()
    library = AnimationLibrary(
        [
            Animation(
                name="speaking",
                steps=(
                    AnimationStep(channel=Channel.EYES, action="expression", args={"emotion": "happy"}),
                ),
            )
        ]
    )
    engine = AnimationEngine(ROBOT_ID, HandlePlayer(recorder), library)
    coordinator = ExpressionCoordinator(ROBOT_ID, recorder, animations=engine, min_interval_s=0.0)

    await coordinator.on_state(AudioState.THINKING, AudioState.SPEAKING)
    await engine.aclose()

    assert "speaking" in [name for name in engine.library.names()]
    assert coordinator.shown == [], "an animation played *and* an expression was set"


async def test_a_missing_animation_leaves_the_face_alone(simulated):
    """No fallback: substituting an expression is how a robot looks surprised every turn."""
    runtime, _ = simulated
    recorder = _Recorder()
    engine = AnimationEngine(ROBOT_ID, HandlePlayer(recorder), AnimationLibrary())
    coordinator = ExpressionCoordinator(ROBOT_ID, recorder, animations=engine, min_interval_s=0.0)

    await coordinator.on_state(AudioState.THINKING, AudioState.SPEAKING)
    await engine.aclose()

    assert coordinator.shown == []
    assert recorder.calls == []


class _Recorder:
    """A semantic handle that records instead of commanding."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        async def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            return None

        return call
