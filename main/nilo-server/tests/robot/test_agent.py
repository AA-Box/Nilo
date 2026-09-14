"""The agent: conversation, tools, refusals, interruption and what happens with no model.

Every test here runs against a **scripted** LLM. There is no network in this file, no API
key, and no assertion that depends on what a real model would say — which is the only way
"the robot behaves correctly when the model asks for something unsafe" can be a test
rather than a hope.

The runtime is real: the executor, the safety policy, the queue and the event bus are the
production ones. Only the model and the device are fakes.
"""
from __future__ import annotations

import asyncio

import pytest

from robot.agent.agent import FALLBACK_REPLIES, AgentSpeaker, LLMUnavailable, RobotAgent
from robot.agent.permissions import ToolPolicy, TurnOrigin
from robot.agent.speech import SpeakIntent, SpeechArbiter, SpeechPriority, RecordingSpeaker
from robot.agent.tools import RobotToolkit
from robot.events.types import AgentTurnCompleted, SpeechRequested, ToolCallRefused
from robot.memory.service import RobotMemory
from robot.memory.store import IN_MEMORY, SqliteMemoryStore
from robot.state.models import RobotBatteryState, RobotSensorState, RobotTelemetry
from tests.robot.conftest import ROBOT_ID, ScriptedLLM, tool_call, until


@pytest.fixture
def agent(agent_runtime):
    runtime, _ = agent_runtime
    return RobotAgent(ROBOT_ID, runtime, llm=ScriptedLLM("Okay."))


def make_agent(runtime, *turns, **kwargs) -> RobotAgent:
    return RobotAgent(ROBOT_ID, runtime, llm=ScriptedLLM(*turns), **kwargs)


@pytest.fixture
async def memory():
    store = SqliteMemoryStore(IN_MEMORY)
    service = RobotMemory(ROBOT_ID, store)
    await service.open()
    try:
        yield service
    finally:
        await service.aclose()
        await store.aclose()


def collect(runtime, event_type):
    """Subscribe and return the list the events land in."""
    seen: list = []
    runtime.events.subscribe(event_type, seen.append)
    return seen


# -- a turn, end to end ---------------------------------------------------------------------------


async def test_an_utterance_becomes_a_tool_call_and_a_spoken_reply(agent_runtime):
    """"come closer" -> robot.move(250mm) -> the device is commanded -> "Okay"."""
    runtime, channel = agent_runtime
    agent = make_agent(runtime, [tool_call("robot_move", distance_mm=250)], "Okay.")

    turn = await agent.respond("come closer", person_id="ahmad")

    assert turn.tool_calls == ("robot_move",)
    assert turn.text == "Okay."
    assert not turn.refusals
    assert await until(lambda: channel.called("robot.motion.move"))
    assert channel.called("robot.motion.move")[0]["distance_mm"] == 250


async def test_the_reply_is_streamed_as_it_arrives(agent_runtime):
    runtime, _ = agent_runtime
    agent = make_agent(runtime, "Hello there.")
    chunks: list[str] = []

    turn = await agent.respond("hi", on_text=chunks.append)

    assert turn.text == "Hello there."
    assert len(chunks) > 1, "the reply arrived in one lump; nothing was streamed"
    assert "".join(chunks) == "Hello there."


async def test_a_turn_publishes_what_it_did(agent_runtime):
    runtime, _ = agent_runtime
    completed = collect(runtime, AgentTurnCompleted)
    agent = make_agent(runtime, [tool_call("robot_set_expression", emotion="happy")], "Done.")

    await agent.respond("look happy")
    await runtime.events.drain()

    assert [event.text for event in completed] == ["Done."]
    assert completed[0].tool_calls == ("robot_set_expression",)


# -- tool validation, through the model -------------------------------------------------------------


async def test_a_model_that_invents_an_argument_is_told_so_and_nothing_moves(agent_runtime):
    runtime, channel = agent_runtime
    agent = make_agent(
        runtime,
        [tool_call("robot_move", distance_mm=250, velocity=5)],
        "Sorry, I got that wrong.",
    )

    turn = await agent.respond("come closer")

    assert turn.refusals and turn.refusals[0].refused_by == "argument_validation"
    assert channel.called("robot.motion.move") == ()
    assert turn.text == "Sorry, I got that wrong."


async def test_the_refusal_is_fed_back_to_the_model(agent_runtime):
    runtime, _ = agent_runtime
    provider = ScriptedLLM([tool_call("robot_move", distance_mm=99_999)], "That is too far.")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)

    await agent.respond("drive to the moon")

    second_round = provider.dialogues[-1]
    tool_messages = [message for message in second_round if message["role"] == "tool"]
    assert tool_messages, "the model was not told what happened to its tool call"
    assert "limit" in tool_messages[-1]["content"]


async def test_a_tool_refusal_is_published(agent_runtime):
    runtime, _ = agent_runtime
    refused = collect(runtime, ToolCallRefused)
    agent = make_agent(runtime, [tool_call("robot_move", distance_mm=99_999)], "No.")

    await agent.respond("drive to the moon")
    await runtime.events.drain()

    assert [event.tool_name for event in refused] == ["robot_move"]
    assert refused[0].refused_by == "argument_validation"


# -- unsafe actions --------------------------------------------------------------------------------


async def test_an_unsafe_action_is_rejected_by_safety_and_the_model_is_told_why(agent_runtime):
    """The robot is on the edge of a table. The model asks it to drive forward anyway."""
    runtime, channel = agent_runtime
    await runtime.update_telemetry(
        ROBOT_ID,
        RobotTelemetry(
            sensors=RobotSensorState(cliff_detected=True, readings={"front_mm": 1500.0}),
            battery=RobotBatteryState(percent=80),
        ),
    )
    provider = ScriptedLLM([tool_call("robot_move", distance_mm=200)], "I cannot, there is a drop.")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)

    turn = await agent.respond("come closer")

    assert turn.refusals
    assert turn.refusals[0].refused_by == "safety:cliff_hazard"
    assert channel.called("robot.motion.move") == (), "an unsafe command reached the device"
    assert turn.text == "I cannot, there is a drop."


async def test_the_model_cannot_talk_its_way_past_an_emergency_stop(agent_runtime):
    runtime, channel = agent_runtime
    await runtime.actions.emergency_stop(ROBOT_ID, "the operator pressed the button")
    channel.calls.clear()
    agent = make_agent(runtime, [tool_call("robot_move", distance_mm=200)], "I am stopped.")

    turn = await agent.respond("please move, it is fine")

    assert turn.refusals[0].refused_by == "safety:emergency_stop_engaged"
    assert channel.called("robot.motion.move") == ()


# -- the model is not there ---------------------------------------------------------------------------


async def test_with_no_provider_the_robot_still_answers(agent_runtime):
    runtime, _ = agent_runtime
    agent = RobotAgent(ROBOT_ID, runtime, llm=None)

    turn = await agent.respond("hello")

    assert turn.used_fallback
    assert turn.text == FALLBACK_REPLIES["unavailable"]
    assert not agent.available


async def test_a_provider_that_raises_falls_back_rather_than_propagating(agent_runtime):
    runtime, _ = agent_runtime
    provider = ScriptedLLM("never reached", error=LLMUnavailable("connection refused"))
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)

    turn = await agent.respond("hello")

    assert turn.fallback == "unavailable"
    assert turn.text == FALLBACK_REPLIES["unavailable"]


async def test_a_provider_that_never_answers_times_out_and_falls_back(agent_runtime):
    runtime, _ = agent_runtime
    provider = ScriptedLLM("a very long answer indeed", delay_s=0.05)
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider, llm_timeout_s=0.05)

    turn = await agent.respond("hello")

    assert turn.fallback == "timeout"


async def test_the_behaviour_engine_is_untouched_by_a_missing_model(agent_runtime):
    """The fallback is a conversation problem. Autonomy has no model in it and carries on."""
    runtime, channel = agent_runtime
    agent = RobotAgent(ROBOT_ID, runtime, llm=None)
    await agent.respond("hello")

    decision = await runtime.behavior(ROBOT_ID).tick()

    assert decision is not None
    assert runtime.behavior(ROBOT_ID).mode is not None


# -- tool timeouts ------------------------------------------------------------------------------------


async def test_a_tool_that_does_not_return_is_abandoned_not_awaited_forever(agent_runtime):
    runtime, channel = agent_runtime
    channel.latency_s = 0.4
    toolkit = RobotToolkit(runtime, ROBOT_ID, timeout_s=0.05)

    outcome = await toolkit.call("robot_capture_image", {})

    assert not outcome.ok
    assert outcome.refused_by == "timeout"


async def test_a_tool_timeout_does_not_end_the_turn(agent_runtime):
    runtime, channel = agent_runtime
    channel.latency_s = 0.4
    toolkit = RobotToolkit(runtime, ROBOT_ID, timeout_s=0.05)
    agent = RobotAgent(
        ROBOT_ID,
        runtime,
        llm=ScriptedLLM([tool_call("robot_capture_image")], "I could not see in time."),
        toolkit=toolkit,
    )

    turn = await agent.respond("what can you see?")

    assert turn.text == "I could not see in time."
    assert turn.refusals[0].refused_by == "timeout"


# -- interruption ---------------------------------------------------------------------------------------


async def test_speech_can_be_interrupted_and_the_conversation_survives(agent_runtime):
    runtime, _ = agent_runtime
    agent = RobotAgent(
        ROBOT_ID,
        runtime,
        llm=ScriptedLLM("This is quite a long answer that gets cut off.", delay_s=0.01),
    )

    task = asyncio.create_task(agent.respond("tell me a story"))
    assert await until(lambda: agent.conversation.active_turn is not None)
    await asyncio.sleep(0.05)
    assert await agent.interrupt("the person started speaking")
    turn = await task

    assert turn.interrupted
    assert turn.text, "nothing was captured before the interruption"
    assert len(turn.text) < len("This is quite a long answer that gets cut off.")
    # The transcript keeps what was actually said out loud, marked as cut off.
    assert any("[interrupted]" in message["content"] for message in agent.conversation.messages)
    assert any(message["role"] == "user" for message in agent.conversation.messages)


async def test_interrupting_when_nothing_is_in_flight_is_a_no_op(agent):
    assert await agent.interrupt() is False


async def test_the_next_utterance_is_answered_after_an_interruption(agent_runtime):
    runtime, _ = agent_runtime
    provider = ScriptedLLM("A long first answer.", "Short second.")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)
    agent.llm.delay_s = 0.01

    task = asyncio.create_task(agent.respond("first"))
    assert await until(lambda: agent.conversation.active_turn is not None)
    await asyncio.sleep(0.03)
    await agent.interrupt()
    await task

    agent.llm.delay_s = 0.0
    second = await agent.respond("second")
    assert second.text == "Short second."
    assert not second.interrupted


# -- conversation ------------------------------------------------------------------------------------


async def test_the_transcript_carries_from_one_turn_to_the_next(agent_runtime):
    runtime, _ = agent_runtime
    provider = ScriptedLLM("Hello.", "Yes, you did.")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)

    await agent.respond("hello", person_id="ahmad")
    await agent.respond("did I say hello?", person_id="ahmad")

    second_dialogue = provider.dialogues[-1]
    said = [message["content"] for message in second_dialogue]
    assert "hello" in said
    assert "Hello." in said


async def test_a_second_person_joins_without_erasing_the_first(agent_runtime):
    runtime, _ = agent_runtime
    provider = ScriptedLLM("Hi.", "Hello to you too.")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)

    await agent.respond("hello", person_id="ahmad")
    await agent.respond("and hello from me", person_id="sam")

    assert agent.conversation.person_id == "sam"
    contents = [message["content"] for message in agent.conversation.messages]
    assert "hello" in contents
    assert any("sam is now the one speaking" in content for content in contents)


async def test_the_transcript_is_bounded(agent_runtime):
    runtime, _ = agent_runtime
    agent = make_agent(runtime, "Fine.")
    for index in range(40):
        await agent.respond(f"question {index}")
    assert len(agent.conversation.messages) <= 20


# -- context ----------------------------------------------------------------------------------------


async def test_the_context_carries_what_the_robot_remembers(agent_runtime, memory):
    runtime, _ = agent_runtime
    await memory.met_person("ahmad", display_name="Ahmad")
    await memory.remember("Ahmad likes the blue cube", person_id="ahmad")
    provider = ScriptedLLM("The blue one.")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider, memory=memory)

    await agent.respond("which cube do I like?", person_id="ahmad")

    prompt = provider.system_prompts[-1]
    assert "blue cube" in prompt
    assert "Ahmad" in prompt


async def test_the_context_has_no_telemetry_history_in_it(agent_runtime):
    """One value per field, never a series. A context that grows with uptime is a bug."""
    runtime, _ = agent_runtime
    provider = ScriptedLLM("Fine.")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)

    for percent in range(80, 60, -1):
        await runtime.update_telemetry(ROBOT_ID, RobotTelemetry(battery=RobotBatteryState(percent=percent)))
    await agent.respond("how are you?")

    prompt = provider.system_prompts[-1]
    assert "61%" in prompt
    assert not any(f"{percent}%" in prompt for percent in range(62, 81))


async def test_the_context_is_bounded(agent_runtime, memory):
    runtime, _ = agent_runtime
    for index in range(200):
        await memory.remember(f"something that happened, number {index}")
    provider = ScriptedLLM("Fine.")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider, memory=memory)

    await agent.respond("what do you remember?")

    assert len(provider.system_prompts[-1]) < 6000


async def test_the_context_names_only_the_tools_this_turn_can_use(agent_runtime):
    runtime, _ = agent_runtime
    provider = ScriptedLLM("Fine.")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)

    await agent.respond("hello", origin=TurnOrigin.BEHAVIOR)

    assert "robot_move" not in provider.system_prompts[-1]
    assert "robot_get_state" in provider.system_prompts[-1]
    assert "robot_move" not in provider.offered


async def test_the_context_says_when_the_robot_is_on_the_edge_of_something(agent_runtime):
    runtime, _ = agent_runtime
    await runtime.update_telemetry(
        ROBOT_ID, RobotTelemetry(sensors=RobotSensorState(cliff_detected=True))
    )
    provider = ScriptedLLM("Careful.")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)

    await agent.respond("come here")

    assert "drop in front of the robot" in provider.system_prompts[-1]


# -- speech arbitration -------------------------------------------------------------------------------


async def test_a_behaviour_triggered_intent_reaches_the_model_and_is_spoken(agent_runtime):
    runtime, _ = agent_runtime
    provider = ScriptedLLM("Hello Ahmad!")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)
    speaker = AgentSpeaker(agent)
    arbiter = SpeechArbiter(ROBOT_ID, speaker, events=runtime.events)

    decision = await arbiter.request(
        SpeakIntent(reason="greeting", target_person_id="ahmad", style="excited"), wait=True
    )

    assert decision.accepted
    assert speaker.said == [("greeting", "Hello Ahmad!")]
    prompt = provider.dialogues[-1][-1]["content"]
    assert "greeting" in prompt and "excited" in prompt and "ahmad" in prompt


async def test_a_behaviour_triggered_turn_is_not_offered_motion_tools(agent_runtime):
    runtime, _ = agent_runtime
    provider = ScriptedLLM("Hello!")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)

    await agent.speak_intent(SpeakIntent(reason="greeting", target_person_id="ahmad"))

    assert "robot_move" not in provider.offered
    assert "robot_set_expression" in provider.offered


async def test_two_things_cannot_speak_at_once(agent_runtime):
    runtime, _ = agent_runtime
    speaker = RecordingSpeaker(delay_s=0.1)
    arbiter = SpeechArbiter(ROBOT_ID, speaker, events=runtime.events)

    first = await arbiter.request(SpeakIntent(reason="greeting"))
    second = await arbiter.request(SpeakIntent(reason="idle_remark"))

    assert first.accepted
    assert not second.accepted
    assert "already saying something" in second.reason
    await arbiter.wait_idle()
    assert [intent.reason for intent in speaker.spoken] == ["greeting"]


async def test_a_safety_announcement_interrupts_whatever_is_being_said(agent_runtime):
    runtime, _ = agent_runtime
    speaker = RecordingSpeaker(delay_s=0.2)
    arbiter = SpeechArbiter(ROBOT_ID, speaker, events=runtime.events)

    await arbiter.request(SpeakIntent(reason="idle_remark", priority=SpeechPriority.AMBIENT))
    decision = await arbiter.request(
        SpeakIntent(reason="cliff", priority=SpeechPriority.SAFETY, text="Careful!"), wait=True
    )

    assert decision.accepted
    assert decision.interrupted == "idle_remark"
    assert [intent.reason for intent in speaker.spoken] == ["cliff"]


async def test_a_safety_line_is_said_exactly_as_written(agent_runtime):
    """No model in the path: a warning that can be paraphrased is a warning that can be
    paraphrased into something else."""
    runtime, _ = agent_runtime
    provider = ScriptedLLM("something else entirely")
    agent = RobotAgent(ROBOT_ID, runtime, llm=provider)

    turn = await agent.speak_intent(
        SpeakIntent(reason="cliff", priority=SpeechPriority.SAFETY, text="Careful, there is a drop!")
    )

    assert turn.text == "Careful, there is a drop!"
    assert provider.calls == 0


async def test_the_same_reason_is_not_repeated_immediately(agent_runtime):
    runtime, _ = agent_runtime
    speaker = RecordingSpeaker()
    arbiter = SpeechArbiter(ROBOT_ID, speaker, events=runtime.events, reason_cooldown_s=60.0)

    await arbiter.request(SpeakIntent(reason="greeting"), wait=True)
    again = await arbiter.request(SpeakIntent(reason="greeting"), wait=True)

    assert not again.accepted
    assert "cooldown" in again.reason


async def test_arbitration_is_observable(agent_runtime):
    runtime, _ = agent_runtime
    requested = collect(runtime, SpeechRequested)
    arbiter = SpeechArbiter(ROBOT_ID, RecordingSpeaker(), events=runtime.events, reason_cooldown_s=60.0)

    await arbiter.request(SpeakIntent(reason="greeting"), wait=True)
    await arbiter.request(SpeakIntent(reason="greeting"), wait=True)
    await runtime.events.drain()

    assert [event.accepted for event in requested] == [True, False]
    assert requested[1].rejected_because


async def test_the_runtime_has_one_arbitration_path(agent_runtime):
    """Everything that wants the robot to talk goes through the same arbiter."""
    runtime, _ = agent_runtime
    speaker = RecordingSpeaker()
    runtime.speech(ROBOT_ID, speaker)

    decision = await runtime.request_speech(ROBOT_ID, SpeakIntent(reason="greeting"))
    await runtime.speech(ROBOT_ID).wait_idle()

    assert decision.accepted
    assert runtime.speech(ROBOT_ID) is runtime.speech(ROBOT_ID)
    assert [intent.reason for intent in speaker.spoken] == ["greeting"]


# -- the runtime seam -----------------------------------------------------------------------------------


async def test_the_runtime_builds_one_agent_per_robot(agent_runtime):
    runtime, _ = agent_runtime
    first = await runtime.agent(ROBOT_ID)
    assert first is await runtime.agent(ROBOT_ID)
    assert first.robot_id == ROBOT_ID


async def test_a_provider_installed_later_reaches_the_agent_that_already_exists(agent_runtime):
    runtime, _ = agent_runtime
    agent = await runtime.agent(ROBOT_ID)
    assert not agent.available

    runtime.set_llm(ScriptedLLM("Hello."))

    assert agent.available
    assert (await agent.respond("hello")).text == "Hello."


async def test_a_policy_configured_on_the_runtime_reaches_the_agent(agent_runtime):
    runtime, _ = agent_runtime
    runtime._tool_policy = ToolPolicy(autonomous=frozenset())
    agent = await runtime.agent(ROBOT_ID)
    assert agent.toolkit.available(TurnOrigin.BEHAVIOR) == ("robot_stop",)
