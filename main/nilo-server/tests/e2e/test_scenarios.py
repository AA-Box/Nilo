"""The ten scenarios, end to end: a real server, a real socket, a real simulated robot.

Each test records a :class:`~tests.e2e.transcript.Transcript` of every state transition
the system published while it ran; the run writes them all to ``tmp/e2e-report.md``.
Nothing reaches into the backend to make an assertion pass — the simulator has only the
frames a device has, and the assertions read the server's own registry, world model,
action registry and event bus.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from robot.actions import ActionStatus
from robot.behavior.base import AutonomyMode
from robot.behavior.engine import BehaviorEngine
from robot.simulator import Scenario, Step
from robot.state.actions import ActionSource
from robot.state.world import Entity, EntityType, Position
from robot.vision.providers import ColourBlobDetector
from robot.vision.types import DetectionKind
from robot.voice.state import AudioState
from tests.e2e.conftest import (
    NORMALIZED,
    E2EBackend,
    make_robot,
    running,
)
from tests.e2e.fakes import RuleBasedLLM

pytestmark = pytest.mark.e2e


# -- A: startup ---------------------------------------------------------------------------


async def test_scenario_a_startup(backend: E2EBackend, scenario: Any) -> None:
    """Connect, authenticate, initialize MCP, discover tools, register, telemetry, world."""
    transcript = scenario(
        "Scenario A — startup",
        "A robot connects: the session is accepted, the device MCP handshake runs, tools "
        "are discovered, the robot is registered, telemetry arrives and the world model "
        "has an entry for it.",
    )
    async with running(make_robot(backend)) as simulator:
        state = await backend.wait_for(lambda: backend.state())
        assert state.is_connected
        assert state.connection.session_id == simulator.session_id
        transcript.note("the robot is registered under the session the server issued")

        await simulator.wait_discovered(timeout=20)
        capabilities = await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED))
        assert capabilities.mcp is True
        assert capabilities.malformed_tools == 0
        assert "robot_motion_move" in capabilities.tool_names
        transcript.note(f"{len(capabilities.tool_names)} tools discovered over device MCP")

        with_telemetry = await backend.wait_for(lambda: _with_battery(backend))
        assert with_telemetry.telemetry.battery is not None
        assert with_telemetry.telemetry.sensors is not None
        assert with_telemetry.telemetry.pose is not None
        transcript.note("battery, pose and sensor telemetry reached the world state")

        world = backend.runtime.world.snapshot(NORMALIZED)
        assert world.robot_id == NORMALIZED
        transcript.note("the world model has a snapshot for this robot")

    assert transcript.of("RobotConnected"), "the connection was never published"
    assert transcript.of("CapabilitiesRefreshed"), "discovery never finished"


# -- B: conversation ------------------------------------------------------------------------


async def test_scenario_b_conversation(backend: E2EBackend, robot: Any, scenario: Any) -> None:
    """A person speaks; the agent answers; the device is told to play it; the face follows."""
    transcript = scenario(
        "Scenario B — conversation",
        "The device reports recognized speech. The robot agent answers through the speech "
        "arbiter, the synthesizer streams it, the device is told to play it, and the audio "
        "state machine walks idle -> thinking -> speaking -> idle.",
    )
    await robot.say("hello there")

    finished = await backend.wait_for(lambda: transcript.of("SpeechFinished"))
    assert "Hello" in finished[0].detail
    transcript.note("the model's answer was spoken through the one speech path")

    speech = backend.speech()
    assert speech is not None
    assert any("Hello" in utterance for utterance in speech.spoken)
    transcript.note("the synthesizer received the answer as a stream, not a single blob")

    await backend.wait_for(lambda: robot.state.speaking is False and speech.streams[0].ended)
    transcript.note("the device saw the speech start and stop")

    states = [line.detail for line in transcript.of("AudioStateChanged")]
    assert any("state=thinking" in detail for detail in states)
    assert any("state=speaking" in detail for detail in states)
    loop = backend.runtime.voice(NORMALIZED)
    assert loop is not None
    assert loop.state is AudioState.IDLE
    transcript.note("the audio state machine ended the turn back in idle")


# -- C: voice-commanded movement -------------------------------------------------------------


async def test_scenario_c_voice_commanded_movement(
    backend: E2EBackend, robot: Any, scenario: Any
) -> None:
    """"Come a little closer": model -> safety -> executor -> device -> completion -> reply."""
    transcript = scenario(
        "Scenario C — voice-commanded movement",
        "One utterance becomes a semantic action, passes the safety policy, is dispatched "
        "over device MCP, moves the simulated robot, and the device's own completion "
        "notification settles the action before the robot answers.",
    )
    started_x = robot.state.x_m
    await robot.say("come a little closer")

    record = await backend.wait_for(lambda: _settled_move(backend), timeout=25)
    assert record.status is ActionStatus.SUCCEEDED
    assert record.source is ActionSource.LLM
    assert record.device_action_id, "the device never acknowledged with an action id"
    transcript.note("the action was attributed to the model and settled by the device")

    assert robot.state.x_m > started_x
    transcript.note(f"the simulated robot moved {robot.state.x_m - started_x:.3f} m")

    await backend.wait_for(lambda: transcript.of("SpeechFinished"), timeout=25)
    transcript.note("the robot acknowledged out loud after the motion settled")

    trace = _one_trace(transcript)
    assert {"UtteranceRecognized", "AgentTurnStarted", "ToolCallStarted", "ActionFinished"} <= trace
    transcript.note("utterance, model turn, device call and completion share one correlation id")


async def _settled_move(backend: E2EBackend) -> Any:
    for record in backend.runtime.actions.list_actions(NORMALIZED, limit=20):
        if record.action_type.value == "move" and record.is_terminal:
            return record
    return None


# -- D: autonomous greeting --------------------------------------------------------------------


async def test_scenario_d_autonomous_greeting(backend: E2EBackend, scenario: Any) -> None:
    """A person appears in the camera; vision creates the entity; GreetPerson wins once."""
    transcript = scenario(
        "Scenario D — autonomous greeting",
        "A person walks into the simulated room. The vision pipeline captures a frame over "
        "device MCP, the local detector finds them in the pixels the camera rendered, the "
        "world model gains a person, and the behaviour engine chooses to greet — once, "
        "because the greeting cooldown holds afterwards.",
    )
    plan = Scenario(
        name="person-enters",
        duration_s=0.0,
        steps=[Step(at_s=1.0, do="spawn_person", args={"id": "p1", "x_mm": 1400, "y_mm": 0})],
    )
    async with running(make_robot(backend, scenario=plan, speed=4.0)) as simulator:
        await simulator.wait_discovered(timeout=20)
        await backend.wait_for(lambda: _ready(backend))

        # Both loops on their own timers, as they run in a server: the perception loop
        # captures through device MCP, and the scheduler ticks against the world model
        # the detections land in. Nothing in this test drives either of them by hand.
        vision = backend.runtime.vision(NORMALIZED, detector=ColourBlobDetector())
        vision.start(interval_s=0.1)
        engine = backend.runtime.behavior(NORMALIZED)
        engine.start(interval_s=0.05)

        detected = await backend.wait_for(lambda: transcript.of("PersonDetected"), timeout=25)
        assert detected
        transcript.note("the detector found the person in the frame the device actually sent")

        world = await backend.wait_for(lambda: _sees_a_person(backend), timeout=15)
        assert world.people
        transcript.note("the world model gained a person entity from the detection")

        greeting = await backend.wait_for(
            lambda: [
                line
                for line in transcript.of("BehaviorSelected")
                if "behavior=greet_person" in line.detail
            ],
            timeout=25,
        )
        transcript.note(f"greet_person won: {greeting[0].detail}")

        looked = await backend.wait_for(lambda: _looked_at_the_person(backend), timeout=25)
        transcript.note(f"the robot pointed its head at them: {looked}")

        await backend.wait_for(
            lambda: [line for line in transcript.of("AnimationStarted") if "excited_greeting" in line.detail],
            timeout=25,
        )
        transcript.note("the greeting animation played")

        completed = await backend.wait_for(
            lambda: [line for line in transcript.of("BehaviorCompleted") if "greet_person" in line.detail],
            timeout=25,
        )
        assert "outcome=completed" in completed[0].detail
        transcript.note("the greeting finished")

        # The cooldown, on the engine's own terms: scoring again without running anything.
        again = engine.evaluate()
        assert again.selected != "greet_person", again.explain()
        assert "greet_person" not in {candidate.name for candidate in again.eligible}
        transcript.note("a second evaluation does not greet again: the cooldown holds")


async def _sees_a_person(backend: E2EBackend) -> Any:
    world = backend.runtime.world.snapshot(NORMALIZED)
    return world if world.people else None


async def _looked_at_the_person(backend: E2EBackend) -> Any:
    """A head command the behaviour engine issued, settled by the device."""
    for record in backend.runtime.actions.list_actions(NORMALIZED, limit=50):
        if record.action_type.value in ("look_at", "head_angle") and record.source is ActionSource.BEHAVIOR:
            return f"{record.action_type.value} {record.status.value}"
    return None


# -- E: boredom ---------------------------------------------------------------------------------


async def test_scenario_e_boredom(backend: E2EBackend, robot: Any, scenario: Any) -> None:
    """Nothing happens for long enough that boredom outranks idle, on a fake clock."""
    transcript = scenario(
        "Scenario E — boredom",
        "No interaction for an accelerated stretch of simulated time. The boredom drive "
        "rises, BoredBehavior becomes eligible and outranks idle, and the robot performs a "
        "low-risk display-only behaviour — it does not drive anywhere.",
    )
    # The engine's own clock, made explicit. Everything else is the runtime's: the real
    # robot handle, the real world model fed by the real device, the real behaviour set.
    # The scheduler is the one component in the subsystem that is allowed to read a clock,
    # so "accelerated simulated time" is exactly this and nothing else.
    elapsed = 0.0
    engine = BehaviorEngine(
        NORMALIZED,
        backend.runtime.actions.robot(NORMALIZED).as_source(ActionSource.BEHAVIOR),
        backend.runtime.world,
        events=backend.runtime.events,
        mode=AutonomyMode.NORMAL,
        clock=lambda: elapsed,
    )
    try:
        engine.note_stimulus("scenario start")
        first = await engine.tick()
        assert first.selected != "bored"
        transcript.note(f"with a fresh stimulus the robot chose {first.selected}")

        elapsed = engine.tuning.boredom_full_s + engine.tuning.boredom_onset_s
        decision = await engine.tick()
        assert decision.selected == "bored", decision.explain()
        transcript.note(
            f"after {elapsed:.0f}s of simulated silence, bored won: {'; '.join(decision.reasons)}"
        )

        behavior = engine.registry.get("bored")
        assert behavior is not None
        assert "drive" not in {resource.value for resource in behavior.required_resources}
        transcript.note("the boredom behaviour claims the display only: nothing drives")
    finally:
        await engine.aclose()


# -- F: cliff protection --------------------------------------------------------------------------


async def test_scenario_f_cliff_stops_the_robot(backend: E2EBackend, scenario: Any) -> None:
    """A cliff appears under a moving robot: the motion fails and the model cannot override it."""
    transcript = scenario(
        "Scenario F — cliff protection",
        "The robot is driving when the floor runs out. The device stops itself and reports "
        "why; the action ends FAILED with the device's reason; and a model asking for "
        "another move while the cliff flag is up is refused by the safety policy, not by "
        "the device.",
    )
    plan = Scenario(
        name="cliff",
        duration_s=0.0,
        steps=[
            Step(at_s=1.0, do="move", args={"distance_mm": 2000, "speed_mmps": 200}),
            Step(
                at_s=2.0,
                do="add_cliff",
                args={"id": "stairs", "x0_mm": 1600, "y0_mm": -500, "x1_mm": 1800, "y1_mm": 3500},
            ),
        ],
    )
    async with running(make_robot(backend, scenario=plan, speed=8.0)) as simulator:
        failures = await backend.wait_for(lambda: transcript.of("MotionFailed"), timeout=30)
        assert "reason=cliff" in failures[0].detail
        assert simulator.state.moving is False
        transcript.note("the device stopped itself and reported the cliff")

        sensors = await backend.wait_for(lambda: _cliff_flag(backend), timeout=20)
        assert sensors.cliff_detected is True
        transcript.note("the cliff reached the world state the safety policy reads")

        # The model asks anyway. This is the assertion the whole layering exists for.
        agent = await backend.runtime.agent(NORMALIZED)
        outcome = await agent.toolkit.call("robot_move", {"distance_mm": 300})
        assert outcome.refused or not outcome.ok
        assert "cliff" in outcome.as_text().lower()
        transcript.note(f"the model's move was refused: {outcome.as_text()}")


async def _cliff_flag(backend: E2EBackend) -> Any:
    state = await backend.state()
    if state is None or state.telemetry.sensors is None:
        return None
    return state.telemetry.sensors if state.telemetry.sensors.cliff_detected else None


# -- G: low battery ---------------------------------------------------------------------------


async def test_scenario_g_low_battery_outranks_everything(
    backend: E2EBackend, scenario: Any
) -> None:
    """Battery falls under the threshold: go_to_charger outranks the sociable behaviours."""
    transcript = scenario(
        "Scenario G — low battery",
        "The simulated battery falls below the charge-seeking threshold. The behaviour "
        "engine re-scores and go_to_charger wins at CRITICAL priority, ahead of every "
        "behaviour that was eligible a tick earlier.",
    )
    plan = Scenario(
        name="battery-drains",
        duration_s=0.0,
        steps=[
            Step(at_s=1.0, do="spawn_person", args={"id": "p1", "x_mm": 1200, "y_mm": 0}),
            Step(at_s=2.0, do="set_battery", args={"percent": 12}),
        ],
    )
    async with running(make_robot(backend, scenario=plan, speed=6.0)) as simulator:
        await simulator.wait_discovered(timeout=20)
        await backend.wait_for(lambda: _battery_below(backend, 20), timeout=25)
        transcript.note("the device reported a battery below the threshold")

        # The robot has to know where a dock is before docking is a thing it can choose.
        # A charger is a LOCATION in the world model; the simulator's camera paints one,
        # but nothing in this repository yet promotes a detected dock to a location, so
        # the test supplies the knowledge and asserts on the arbitration (PROJECT_STATUS.md).
        await backend.runtime.world.observe(
            NORMALIZED,
            Entity(
                id="dock-1",
                type=EntityType.LOCATION,
                position=Position(distance_mm=1500, bearing_deg=0.0),
                attributes={"label": "dock"},
            ),
        )
        engine = backend.runtime.behavior(NORMALIZED)
        decision = await backend.wait_for(lambda: _charges(engine), timeout=25)
        assert decision.selected == "go_to_charger"
        transcript.note(
            "go_to_charger scored %.2f, ahead of %s"
            % (
                decision.score,
                ", ".join(
                    f"{candidate.name} {candidate.total:.2f}" for candidate in decision.alternatives[:3]
                )
                or "nothing else eligible",
            )
        )
        behavior = engine.registry.get("go_to_charger")
        assert behavior is not None and int(behavior.priority) >= 90
        transcript.note("it wins on priority, not on a tuning number somebody can nudge")


def _charges(engine: Any) -> Any:
    async def tick() -> Any:
        decision = await engine.tick()
        return decision if decision.selected == "go_to_charger" else None

    return tick()


async def _battery_below(backend: E2EBackend, percent: int) -> Any:
    state = await backend.state()
    if state is None or state.telemetry.battery is None:
        return None
    return state if state.telemetry.battery.percent <= percent else None


# -- H: interruption ---------------------------------------------------------------------------


async def test_scenario_h_barge_in(backend: E2EBackend, robot: Any, scenario: Any) -> None:
    """The person talks over the robot: speech stops, the robot listens, the next turn runs."""
    transcript = scenario(
        "Scenario H — interruption",
        "The robot is speaking when the person starts talking. The device sends abort; the "
        "speech stream is cancelled; the audio state machine goes speaking -> interrupted "
        "-> listening (never straight to idle); and the next utterance is answered normally.",
    )
    backend.runtime.set_llm(RuleBasedLLM(delay_s=0.02))
    agent = await backend.runtime.agent(NORMALIZED)
    agent.llm = backend.runtime.llm

    await robot.say("hello there, tell me something")
    await backend.wait_for(lambda: transcript.of("SpeechStarted"), timeout=25)
    await robot.interrupt()

    await backend.wait_for(lambda: transcript.of("SpeechInterrupted") or _interrupted(transcript), timeout=25)
    loop = backend.runtime.voice(NORMALIZED)
    assert loop is not None
    path = [state.value for state in loop.states.path]
    assert "interrupted" in path, path
    assert path.index("interrupted") < len(path)
    transcript.note(f"audio path: {' -> '.join(path)}")
    assert loop.state is AudioState.LISTENING
    transcript.note("the robot is listening again, not idle")

    speech = backend.speech()
    assert speech is not None
    before = len(speech.streams)
    await robot.say("come a little closer")
    await backend.wait_for(lambda: len(speech.streams) > before, timeout=25)
    transcript.note("the utterance after the barge-in was answered normally")


def _interrupted(transcript: Any) -> Any:
    return [line for line in transcript.of("SpeechFinished") if "interrupted=True" in line.detail]


# -- I: the model is gone ------------------------------------------------------------------------


async def test_scenario_i_the_robot_survives_a_dead_model(
    backend: E2EBackend, scenario: Any
) -> None:
    """No LLM at all: the robot still connects, reports, obeys safety, and behaves."""
    transcript = scenario(
        "Scenario I — backend/LLM failure",
        "The model is unavailable. Everything that is not conversation carries on: the "
        "robot connects, telemetry flows, an operator command still executes, the safety "
        "policy still refuses what it refused before, and the deterministic behaviour "
        "engine still chooses and runs behaviours.",
    )
    backend.runtime.set_llm(RuleBasedLLM(error=RuntimeError("the model is unreachable")))

    async with running(make_robot(backend)) as simulator:
        await simulator.wait_discovered(timeout=20)
        state = await backend.wait_for(lambda: _ready(backend))
        assert state.is_connected
        transcript.note("the robot connected and reported telemetry with no model behind it")

        record = await backend.runtime.robot(NORMALIZED).as_source(ActionSource.USER).move(distance_mm=200)
        settled = await backend.wait_for(lambda: _terminal(backend, record.action_id), timeout=25)
        assert settled.status is ActionStatus.SUCCEEDED
        transcript.note("a direct operator command executed end to end")

        engine = backend.runtime.behavior(NORMALIZED)
        decision = await engine.tick()
        assert decision.selected is not None
        transcript.note(f"the behaviour engine still decided: {decision.selected}")

        agent = await backend.runtime.agent(NORMALIZED)
        turn = await agent.respond("hello there")
        assert turn.used_fallback
        transcript.note(f"the model's absence is a sentence, not an exception: {turn.text!r}")

        blocked = await backend.runtime.robot(NORMALIZED).as_source(ActionSource.LLM).move(distance_mm=99_000)
        assert blocked.status is ActionStatus.REJECTED
        transcript.note(f"safety is unchanged with the model gone: {blocked.error.code}")


async def _terminal(backend: E2EBackend, action_id: str) -> Any:
    record = backend.runtime.actions.query(action_id)
    return record if record is not None and record.is_terminal else None


# -- J: reconnect --------------------------------------------------------------------------------


async def test_scenario_j_reconnect(backend: E2EBackend, scenario: Any) -> None:
    """The network drops: the robot stops locally, the server marks it gone, then it returns."""
    transcript = scenario(
        "Scenario J — reconnect",
        "The socket dies mid-move with no close frame. The device stops itself, the server "
        "marks the robot disconnected and cancels its actions, and when the device comes "
        "back it is a new session whose capabilities are rediscovered from scratch.",
    )
    # A slower clock than the other scenarios on purpose: the socket has to die while the
    # robot is still driving, and at CLOCK_SPEED an 800 mm move is over in 200 ms.
    simulator = make_robot(backend, reconnect=True, reconnect_delay_s=0.2, speed=3.0)
    async with running(simulator):
        await simulator.wait_discovered(timeout=20)
        first = await backend.wait_for(lambda: backend.state())
        await backend.wait_for(lambda: _ready(backend))

        # `wait=False`: this scenario is about an action that is still in flight when the
        # socket dies, and the handle's default is to wait for the device to finish.
        moving = await backend.runtime.robot(NORMALIZED).as_source(ActionSource.USER).move(
            distance_mm=800, speed_mmps=60, wait=False
        )
        assert moving.status is not ActionStatus.REJECTED, moving.error
        await backend.wait_for(lambda: simulator.state.moving)
        await simulator.drop_connection(reconnect=True)

        gone = await backend.wait_for(lambda: _disconnected(backend), timeout=25)
        assert gone.connection.is_connected is False
        assert await backend.runtime.capabilities.get(NORMALIZED) is None
        transcript.note("the server dropped the capabilities with the session")

        cancelled = await backend.wait_for(lambda: _terminal(backend, moving.action_id), timeout=25)
        assert cancelled.status in (ActionStatus.CANCELLED, ActionStatus.FAILED, ActionStatus.TIMED_OUT)
        transcript.note(f"the in-flight action ended {cancelled.status.value} rather than hanging")
        await backend.wait_for(lambda: not simulator.state.moving, timeout=20)
        transcript.note(
            "the device stopped itself %.1fs after the link went away, with nobody to ask"
            % (simulator.config.link_grace_ms / 1000.0)
        )

        back = await backend.wait_for(
            lambda: _reconnected(backend, first.connection.session_id), timeout=30
        )
        assert back.connection.reconnect_count >= 1
        capabilities = await backend.wait_for(
            lambda: backend.runtime.capabilities.get(NORMALIZED), timeout=25
        )
        assert "robot_motion_move" in capabilities.tool_names
        transcript.note("a new session, and capabilities rediscovered on it")

        healthy = await backend.wait_for(lambda: _ready(backend), timeout=25)
        assert healthy.is_connected
        transcript.note("the robot is healthy again: connected, discovered, reporting")


async def _disconnected(backend: E2EBackend) -> Any:
    state = await backend.state()
    return state if state is not None and not state.connection.is_connected else None


async def _reconnected(backend: E2EBackend, previous_session: str) -> Any:
    state = await backend.state()
    if state is None or not state.connection.is_connected:
        return None
    return state if state.connection.session_id != previous_session else None


# -- shared helpers -------------------------------------------------------------------------------


async def _ready(backend: E2EBackend) -> Any:
    state = await backend.state()
    if state is None or state.telemetry.sensors is None:
        return None
    return state if state.capabilities.has_tool("robot_motion_move") else None


async def _with_battery(backend: E2EBackend) -> Any:
    state = await backend.state()
    return state if state is not None and state.telemetry.battery is not None else None


def _one_trace(transcript: Any) -> set[str]:
    """The event names of the busiest correlation id. One utterance's whole chain."""
    traces = transcript.traces()
    if not traces:
        return set()
    busiest = max(traces.values(), key=len)
    return {line.event for line in busiest}
