"""Metrics, correlation ids and the trace, against a running system.

The unit suite proves the registry arithmetic. This file proves the wiring: that a robot
connecting moves a gauge, that a device tool call lands in a histogram, and that one
utterance produces one trace a person can follow from the words to the device's answer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from robot import observability as obs
from robot.correlation import correlate, current_correlation_id, new_correlation_id
from robot.events.types import RobotEvent
from robot.metrics import MetricRegistry
from robot.state.actions import ActionSource
from tests.e2e.conftest import NORMALIZED, E2EBackend, make_robot, running

pytestmark = pytest.mark.e2e


async def test_connecting_and_disconnecting_moves_the_gauge(backend: E2EBackend) -> None:
    metrics = backend.runtime.metrics
    async with running(make_robot(backend)) as simulator:
        await simulator.wait_discovered(timeout=20)
        await backend.wait_for(lambda: metrics.value(obs.ROBOTS_CONNECTED) == 1.0)
        assert metrics.value(obs.SESSIONS_TOTAL, reconnect="false") >= 1
        # `wait_discovered` proves the *device* served tools/list, not that the server has
        # finished recording it — the count moves a moment later.
        await backend.wait_for(lambda: metrics.value(obs.DISCOVERY_TOTAL, mcp="true") >= 1)
    await backend.wait_for(lambda: metrics.value(obs.ROBOTS_CONNECTED) == 0.0, timeout=20)
    assert sum(metrics.snapshot()["counters"][obs.DISCONNECTS_TOTAL].values()) >= 1


async def test_a_tool_call_and_an_action_are_measured(backend: E2EBackend, robot: Any) -> None:
    metrics = backend.runtime.metrics
    json.loads(await backend.runtime.call_tool(NORMALIZED, "robot_get_status", {}))
    await backend.wait_for(lambda: metrics.value(obs.TOOL_CALLS_TOTAL, tool="robot_get_status") >= 1)

    record = await backend.runtime.robot(NORMALIZED).as_source(ActionSource.USER).move(distance_mm=200)
    await backend.wait_for(
        lambda: metrics.value(obs.ACTIONS_TOTAL, action_type="move", status=record.status.value) >= 1
    )
    await backend.wait_for(lambda: metrics.value(obs.ACTION_LATENCY, action_type="move") >= 1)

    rendered = metrics.render()
    assert f"# TYPE {obs.TOOL_LATENCY} histogram" in rendered
    assert f'{obs.TOOL_LATENCY}_bucket{{tool="robot_get_status",le="+Inf"}}' in rendered
    assert f"{obs.ROBOTS_CONNECTED} 1" in rendered


async def test_a_conversation_is_measured_end_to_end(backend: E2EBackend, robot: Any) -> None:
    metrics = backend.runtime.metrics
    await robot.say("come a little closer")
    await backend.wait_for(lambda: metrics.value(obs.LLM_TURNS_TOTAL, outcome="answered") >= 1, timeout=25)
    await backend.wait_for(lambda: metrics.value(obs.LLM_LATENCY) >= 1)
    await backend.wait_for(lambda: metrics.value(obs.TTS_STREAMS_TOTAL, outcome="completed") >= 1)
    await backend.wait_for(lambda: metrics.value(obs.TTS_TIME_TO_FIRST_AUDIO) >= 1)
    assert metrics.value(obs.ASR_UTTERANCES_TOTAL) >= 1


async def test_asr_latency_is_recorded_when_the_session_reports_it(
    backend: E2EBackend, robot: Any
) -> None:
    """The one measurement that comes from the inherited pipeline rather than from an event.

    ``core/providers/asr/base.py`` leaves the recognizer's elapsed time on the connection
    and ``robot/voice/seam.py`` reads it. This test writes the attribute the same way that
    line does, because running a real recognizer needs a model download CI does not have.
    """
    from robot.voice.seam import ASR_LATENCY_ATTR

    await robot.say("hello there")
    connection = await backend.wait_for(lambda: backend.connection())
    await backend.wait_for(lambda: backend.runtime.metrics.value(obs.ASR_UTTERANCES_TOTAL) >= 1)

    setattr(connection, ASR_LATENCY_ATTR, 412.0)
    await robot.say("come a little closer")
    await backend.wait_for(lambda: backend.runtime.metrics.value(obs.ASR_LATENCY) >= 1, timeout=25)
    histogram = backend.runtime.metrics.snapshot()["histograms"][obs.ASR_LATENCY][""]
    assert histogram["sum"] == pytest.approx(0.412, abs=0.001)


async def test_one_utterance_is_one_trace_from_words_to_device_response(
    backend: E2EBackend, robot: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The requirement in one assertion: every link in the chain shares a correlation id."""
    seen: list[RobotEvent] = []
    backend.runtime.events.subscribe(RobotEvent, seen.append)

    with caplog.at_level(logging.INFO, logger="robot.trace"):
        await robot.say("come a little closer")
        await backend.wait_for(lambda: _completed_move(seen), timeout=25)

    traces: dict[str, set[str]] = {}
    for event in seen:
        if event.correlation_id:
            traces.setdefault(event.correlation_id, set()).add(event.name)
    assert traces, "nothing was correlated"
    chain = max(traces.values(), key=len)
    assert {
        "UtteranceRecognized",
        "AgentTurnStarted",
        "AgentTurnCompleted",
        "ToolCallStarted",
        "ToolCallCompleted",
        "ActionSubmitted",
        "ActionStarted",
        "ActionFinished",
    } <= chain, sorted(chain)

    # And the same chain is readable in the log, which is what an incident actually has.
    trace_id = max(traces, key=lambda key: len(traces[key]))
    lines = [record.getMessage() for record in caplog.records if record.name == "robot.trace"]
    ours = [line for line in lines if f"correlation_id={trace_id}" in line]
    assert any("event=UtteranceRecognized" in line for line in ours)
    assert any("event=ToolCallCompleted" in line and "tool_name=robot_motion_move" in line for line in ours)
    assert any("event=ActionFinished" in line and "device_action_id=" in line for line in ours)


def _completed_move(seen: list[RobotEvent]) -> Any:
    return [
        event
        for event in seen
        if event.name == "ActionFinished"
        and getattr(event, "action", None) is not None
        and event.action.action_type.value == "move"
        and event.action.is_terminal
    ]


async def test_a_behaviour_decision_is_counted(backend: E2EBackend, robot: Any) -> None:
    engine = backend.runtime.behavior(NORMALIZED)
    decision = await engine.tick()
    assert decision.selected is not None
    await backend.wait_for(
        lambda: backend.runtime.metrics.value(obs.BEHAVIOR_DECISIONS_TOTAL, behavior=decision.selected) >= 1
    )


async def test_the_metrics_endpoint_is_authorized_and_the_probes_are_not(
    backend: E2EBackend, robot: Any
) -> None:
    """``/metrics`` needs the admin token; ``/health`` and ``/ready`` never do."""
    test_utils = pytest.importorskip("aiohttp.test_utils")
    from robot.api import build_app

    async def memories(robot_id: str) -> Any:
        return await backend.runtime.memory(robot_id)

    app = build_app(backend.runtime, memories, token="observability-token")
    server = test_utils.TestServer(app)
    client = test_utils.TestClient(server)
    await client.start_server()
    try:
        assert (await client.get("/health")).status == 200
        ready = await client.get("/ready")
        assert ready.status == 200
        assert (await ready.json())["status"] == "ready"

        assert (await client.get("/metrics")).status == 401
        scrape = await client.get("/metrics", headers={"Authorization": "Bearer observability-token"})
        assert scrape.status == 200
        assert "nilo_robot_" in await scrape.text()

        payload = await (
            await client.get("/api/metrics", headers={"Authorization": "Bearer observability-token"})
        ).json()
        assert obs.ROBOTS_CONNECTED in payload["gauges"]
    finally:
        await client.close()


async def test_a_closed_runtime_is_not_ready(backend: E2EBackend) -> None:
    test_utils = pytest.importorskip("aiohttp.test_utils")
    from robot.api import build_app

    async def memories(robot_id: str) -> Any:
        return None

    app = build_app(backend.runtime, memories, token="observability-token")
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()
    try:
        await backend.runtime.aclose()
        response = await client.get("/ready")
        assert response.status == 503
        assert (await response.json())["status"] == "closed"
    finally:
        await client.close()


# -- the correlation primitive itself ---------------------------------------------------------


def test_correlate_inherits_an_existing_trace_and_restores_the_previous_one() -> None:
    assert current_correlation_id() == ""
    with correlate(new_correlation_id()) as outer:
        assert current_correlation_id() == outer
        with correlate() as inner:
            assert inner == outer, "a nested correlate must not start a second trace"
        with correlate("joined-from-elsewhere"):
            assert current_correlation_id() == "joined-from-elsewhere"
        assert current_correlation_id() == outer
    assert current_correlation_id() == ""


def test_an_event_stamps_the_trace_in_scope() -> None:
    from robot.events.types import SpeechStarted

    assert SpeechStarted(robot_id="r").correlation_id == ""
    with correlate("trace-1"):
        assert SpeechStarted(robot_id="r").correlation_id == "trace-1"


def test_the_observer_survives_an_event_it_cannot_measure() -> None:
    """A metric is never worth losing an event over, so the fold swallows its own failures."""
    registry = MetricRegistry()
    observer = obs.RobotObserver(registry, trace=False)

    class Odd(RobotEvent):
        pass

    observer.observe(Odd(robot_id="r"))
    assert registry.value(obs.EVENTS_TOTAL, event="Odd") == 1
